"""
mes.py
======
In-memory MES. No database -- persistence is handled externally.

Architecture
------------
  OrderReceiver (order_receiver.py)  --  TCP server, port 6666
      |  callback: on_client_order(ClientOrder)
      v
  MES._orders  :  list[ActiveOrder], sorted by priority after every change
      |  scheduler loop: pick highest-priority, dispatch to PLC, wait
      v
  PLCInterface  --  OPC-UA to CODESYS

Priority formula  (from orders.py ActiveOrder.calculate_priority)
-----------------
  score = (penalty / estimated_time_remaining) * boost
  estimated_time_remaining = qty_remaining * ESTIMATED_TIME[piece_type]
  boost = IN_PROGRESS_BOOST when status == "IN_PROGRESS", else 1.0

  Higher score = produced next.

Warehouse / day cycle
---------------------
  At most WAREHOUSE_CAP pieces are dispatched per day cycle.
  Every UNLOAD_INTERVAL seconds the day counter resets ("end of day").
  Pieces are routed to the unloading dock (U) as they are produced --
  no explicit W2 move is needed.

Remote orders
-------------
  Any machine on the network can send orders to port 6666 using the
  same JSON format as order_generator.py.
"""

import logging
import threading
import time
from typing import Optional

from orders import (
    ActiveOrder, ClientOrder, Order as ProdOrder,
    VALID_TYPES, ESTIMATED_TIME,
)
try:
    from db_handler import (
        update_order_status, update_tool_usage, update_machine_stats,
        update_unload_stats
    )
except Exception:
    # db_handler may not be available in some test contexts
    def update_order_status(order_id, status):
        return
    def update_tool_usage(machine_id, tool_name, total_time_s, pieces_processed):
        return
    def update_machine_stats(machine_id, total_op_time_s, occupation_pct, tool_changes, pieces_total):
        return
    def update_unload_stats(dock_id, piece_type, count):
        return

logger = logging.getLogger(__name__)

# ── Tuning ────────────────────────────────────────────────────────────────────

IN_PROGRESS_BOOST = 1.5    # priority multiplier for in-progress orders
WAREHOUSE_CAP     = 20     # max pieces dispatched per day cycle
UNLOAD_INTERVAL   = 60.0   # simulated day length (real seconds)
SCHEDULER_POLL    = 1.5    # main loop sleep (seconds)
PRODUCTION_POLL   = 1.0    # how often to poll PLC procedures (seconds)
PRODUCTION_TIMEOUT= 300.0  # hard timeout per batch (seconds)
BATCH_SIZE        = 1      # pieces dispatched per PLC trigger (1 = sequential)
                           # Each trigger writes BATCH_SIZE*13 slots at once so
                           # the PLC tracks all procedures together.  Increase to
                           # 4 for 4-cell parallelism; decrease to 1 if overloading.
W1_THRESHOLD      = 20     # pause when W1 has this many pieces (cap = 32)
ORDER_HOST        = "0.0.0.0"
ORDER_PORT        = 6666


# ── Display ID counter ────────────────────────────────────────────────────────

_ao_id_counter = 0
_ao_id_lock    = threading.Lock()

def _next_ao_id() -> int:
    global _ao_id_counter
    with _ao_id_lock:
        _ao_id_counter += 1
        return _ao_id_counter


# ── MES ───────────────────────────────────────────────────────────────────────

class MES:
    """
    In-memory MES scheduler.

    Typical use (from main.py):
        mes = MES(plc)
        mes.start_receiver()   # start TCP order receiver daemon
        mes.run()              # blocks until stop()

    Test use (from test_recipes.py):
        mes = MES()            # plc=None is fine; only add_materials() is used
    """

    def __init__(self, plc=None,
                 order_host: str = ORDER_HOST,
                 order_port: int = ORDER_PORT):
        self._plc        = plc
        self._lock       = threading.Lock()
        self._orders: list[ActiveOrder] = []
        self._pieces_today: int = 0   # reset every UNLOAD_INTERVAL
        self._stop       = threading.Event()
        self._start_ts   = time.time()
        self._order_host = order_host
        self._order_port = order_port
        self._receiver   = None

        # Stats
        self._dispatched = 0
        self._failed     = 0
        self._day_cycles = 0
        
        # Statistics tracking for Requirement 4.3
        self._completed_procedures = set()  # Track recorded procedure IDs
        self._cell_tool_usage = {}          # {cell: {tool: {'time': s, 'pieces': n}}}
        self._cell_occupation_start = {}    # {cell: start_time}
        self._cell_total_time = {}          # {cell: total_seconds}
        self._unload_counts = {}            # {piece_type: count}

        # Start unload timer
        threading.Thread(target=self._unload_loop, daemon=True,
                         name="unload-timer").start()

    # ── Order ingestion ───────────────────────────────────────────────────────

    def on_client_order(self, client_order: ClientOrder, db_order_ids: list | None = None):
        """
        Callback for OrderReceiver.
        Converts each line in the ClientOrder to an ActiveOrder and
        inserts it into the priority queue immediately.
        """
        now = time.time()
        added = []
        with self._lock:
            for idx, o in enumerate(client_order.orders):
                ao = ActiveOrder(
                    client_order_id = client_order.OrderID,
                    piece_type      = o.type.upper(),
                    quantity        = o.quantity,
                    ddate_days      = o.DDate,
                    penalty         = o.Penalty,
                    status          = "PENDING",
                    started_at      = now,
                )
                # prefer DB provided id (when restoring or after saving) otherwise generate internal id
                if db_order_ids and idx < len(db_order_ids) and db_order_ids[idx]:
                    ao.db_order_id = db_order_ids[idx]
                else:
                    ao.db_order_id = _next_ao_id()
                ao.calculate_priority(IN_PROGRESS_BOOST)
                self._orders.append(ao)
                added.append(ao)
            self._sort_locked()

        for ao in added:
            _log(f"[queue] #{ao.db_order_id}  "
                 f"{ao.quantity}×{ao.piece_type}  "
                 f"DDate={ao.ddate_days}d  Penalty={ao.penalty}  "
                 f"score={ao.priority:.3f}  ({client_order.name})")
        self._print_queue()

    def add_materials(self, wood: int = 0, metal: int = 0):
        """
        Stub for test_recipes.py compatibility.
        Raw material loading is handled externally (SFS loading docks).
        """
        _log(f"[materials] add_materials called: wood={wood} metal={metal}  "
             f"-- please load manually via SFS loading docks")

    # ── Receiver startup ──────────────────────────────────────────────────────

    def start_receiver(self):
        """Start the TCP order receiver in a daemon thread."""
        from order_receiver import OrderReceiver
        self._receiver = OrderReceiver(
            host              = self._order_host,
            port              = self._order_port,
            on_order_received = self.on_client_order,
        )
        self._receiver.start_server()
        threading.Thread(
            target = self._receiver.receive_orders,
            daemon = True,
            name   = "order-receiver",
        ).start()

    # ── Main scheduler loop ───────────────────────────────────────────────────

    def run(self):
        """
        Scheduler loop -- blocks until stop() is called.

        Each tick:
          1. Recalculate + re-sort priorities.
          2. Check day cap and W1 warehouse level.
          3. Build a batch of up to BATCH_SIZE highest-priority pieces.
          4. Write all batch slots in ONE trigger (BATCH_SIZE*13 slots).
          5. Poll until ALL batch procedures clear.
          6. Credit every piece in the batch.

        Writing multiple recipes in one trigger keeps MES_Procedures[0..N*13-1]
        filled for the entire batch -- no overwrite between pieces.
        """
        _banner("MES scheduler running -- Ctrl+C or 'exit' to stop")

        while not self._stop.is_set():
            if self._plc is None:
                time.sleep(SCHEDULER_POLL)
                continue

            with self._lock:
                self._recalculate_locked()
                self._sort_locked()
                capped = (self._pieces_today >= WAREHOUSE_CAP)
                batch_orders = self._pick_batch_locked()

            if not batch_orders:
                time.sleep(SCHEDULER_POLL)
                continue

            if capped:
                _log(f"[scheduler] Day cap "
                     f"({self._pieces_today}/{WAREHOUSE_CAP}) "
                     f"-- waiting for unload")
                time.sleep(SCHEDULER_POLL)
                continue

            # Check W1 has room for BATCH_SIZE * 3 more raw pieces
            try:
                w1 = self._plc.get_warehouse_status()["W1"]
                if w1 >= W1_THRESHOLD:
                    _log(f"[scheduler] W1 near capacity "
                         f"({w1}/{W1_THRESHOLD}) -- waiting")
                    time.sleep(SCHEDULER_POLL)
                    continue
            except Exception as exc:
                _log(f"[scheduler] W1 read error: {exc}")

            # Build + dispatch batch
            self._print_queue()
            types_str = " + ".join(o.piece_type for o in batch_orders)
            _log(f"[scheduler] Dispatching batch of {len(batch_orders)}: "
                 f"{types_str}")

            success = self._dispatch_batch_and_wait(batch_orders)

            with self._lock:
                for order in batch_orders:
                    if success:
                        order.quantity_done += 1
                        self._pieces_today += 1
                        self._dispatched += 1
                        if order.quantity_done >= order.quantity:
                            order.status = "COMPLETED"
                            _log(f"[scheduler] ✓ Order #{order.db_order_id} "
                                 f"COMPLETE ({order.quantity}×{order.piece_type})")
                            try:
                                update_order_status(order.db_order_id, order.status)
                            except Exception:
                                pass
                        else:
                            order.status = "IN_PROGRESS"
                            _log(f"[scheduler] ✓ #{order.db_order_id} "
                                 f"{order.quantity_done}/{order.quantity}  "
                                 f"today={self._pieces_today}/{WAREHOUSE_CAP}")
                            try:
                                update_order_status(order.db_order_id, order.status)
                            except Exception:
                                pass
                    else:
                        self._failed += 1
                        order.status = "PENDING"
                        try:
                            update_order_status(order.db_order_id, order.status)
                        except Exception:
                            pass
                self._recalculate_locked()
                self._sort_locked()

            time.sleep(SCHEDULER_POLL)

    def stop(self):
        self._stop.set()
        if self._receiver is not None:
            try:
                self._receiver.stop_server()
            except Exception:
                pass

    # ── Unload loop ───────────────────────────────────────────────────────────

    def _unload_loop(self):
        time.sleep(UNLOAD_INTERVAL)
        while not self._stop.is_set():
            self._do_unload()
            time.sleep(UNLOAD_INTERVAL)

    def _do_unload(self):
        with self._lock:
            count = self._pieces_today
        self._day_cycles += 1
        
        # Record machine statistics for each cell (end-of-day reporting)
        for cell_name, tools_data in self._cell_tool_usage.items():
            total_time = sum(t["time"] for t in tools_data.values())
            total_pieces = sum(t["pieces"] for t in tools_data.values())
            occupation_pct = (total_time / UNLOAD_INTERVAL * 100) if UNLOAD_INTERVAL > 0 else 0
            tool_changes = len([t for t in tools_data.values() if t["pieces"] > 0])
            
            try:
                update_machine_stats(
                    machine_id=cell_name,
                    total_op_time_s=total_time,
                    occupation_pct=occupation_pct,
                    tool_changes=tool_changes,
                    pieces_total=total_pieces
                )
            except Exception as e:
                _log(f"[stats] Error recording machine stats for {cell_name}: {e}")
        
        _log(f"[unload] Day #{self._day_cycles} end -- "
             f"{count} piece(s) on unload docks -- resetting counter")
        with self._lock:
            self._pieces_today = 0
            self._cell_tool_usage = {}
            self._unload_counts = {}

    # ── Production ────────────────────────────────────────────────────────────

    def _dispatch_batch_and_wait(self, orders: list) -> bool:
        """
        Build one recipe per order, concatenate all slots into a single
        write+trigger.  The PLC fills MES_Procedures[0..N*13-1] for the
        entire batch, so all pieces are tracked in one poll cycle.

        Returns True when all batch procedures clear (all pieces done).
        """
        from opcua_handler import build_recipe
        all_slots = []
        for order in orders:
            id_rec, id_proc, id_piece, id_final = self._plc._alloc_ids()
            slots = build_recipe(
                piece_type         = order.piece_type,
                id_recipe          = id_rec,
                id_procedure_start = id_proc,
                id_piece_start     = id_piece,
                id_final_piece     = id_final,
            )
            all_slots.extend(slots)

        n_slots = len(all_slots)
        try:
            self._plc._handler.write_procedure_limits(n_slots)
            if not self._plc._handler.write_recipe(all_slots):
                _log("[dispatch] Recipe write failed")
                return False
            if not self._plc._handler.trigger_recipe():
                _log("[dispatch] PLC did not acknowledge trigger")
                return False
        except Exception as exc:
            _log(f"[dispatch] Exception: {exc}")
            return False

        _log(f"[dispatch] PLC ack -- {n_slots} slots "
             f"({len(orders)} piece(s)) -- polling...")
        return self._wait_for_completion(
            timeout   = PRODUCTION_TIMEOUT,
            max_slots = n_slots + 5,   # enough to see all batch procedures
        )

    def _wait_for_completion(self, timeout: float,
                             max_slots: int = 15) -> bool:
        """
        Poll read_procedures(max_slots) every second until all clear.
        Same logic as test_recipes.poll_result -- returns True when done.
        Records procedure statistics (tool usage, unload counts) as they complete.
        """
        time.sleep(2.0)   # give PLC a moment to start
        start      = time.time()
        prev_count = -1
        saw_active = False

        while (time.time() - start) < timeout:
            if self._stop.is_set():
                return False
            try:
                procs   = self._plc._handler.read_procedures(
                              max_slots=max_slots)
                errors  = self._plc.get_errors()
                n       = len(procs)
                elapsed = int(time.time() - start)

                for e in errors:
                    _log(f"[production] PLC error "
                         f"code={e['code']} proc={e['procedure_id']}")

                if n > 0:
                    proc_info = ", ".join(
                        f"id={p.get('id')} status={p.get('status')} cell={p.get('cell')} "
                        f"tool={p.get('tool')} tt={p.get('tool_time')} type={p.get('piece_type')}"
                        for p in procs
                    )
                    _log(f"[production] procedures: {proc_info}")

                # Record statistics for completed procedures
                self._record_procedure_statistics(procs)

                if n != prev_count:
                    _log(f"[production]   [{elapsed:3d}s] procs={n}")
                    prev_count = n

                if n > 0:
                    saw_active = True
                if n == 0 and elapsed > 2:
                    return True

            except Exception as exc:
                _log(f"[production] Poll error: {exc}")

            time.sleep(PRODUCTION_POLL)

        _log(f"[production] Timeout after {timeout:.0f}s "
             f"(saw_active={saw_active})")
        return saw_active

    def _record_procedure_statistics(self, procedures: list):
        """
        Record machine, tool, and unload statistics from completed procedures.
        
        Cell mapping:  100=C1, 200=C2, 300=C3, 400=C4
        Unload:        Cell=40 (U)
        Tool mapping:  0=IDLE, 1-6=T1-T6, 8-11=T8-T11
        """
        from opcua_handler import EProcedureStatus, ELocation, ETool
        
        for proc in procedures:
            proc_id = proc.get("id")
            
            # Skip if already recorded
            if proc_id in self._completed_procedures:
                continue
            
            # Only record completed procedures
            if proc.get("status") != EProcedureStatus.COMPLETED:
                continue
            
            # Mark as recorded
            self._completed_procedures.add(proc_id)
            
            cell = proc.get("cell", 0)
            tool = proc.get("tool", 0)
            tool_time = proc.get("tool_time", 0)
            piece_type = proc.get("piece_type", 0)
            piece_material = proc.get("piece_material", 0)
            
            # Map cell ID to machine name (use cell ID as key for now)
            cell_name = f"C{cell // 100}" if 100 <= cell <= 400 else f"CELL_{cell}"
            _log(f"[stats] Procedure completed proc_id={proc_id} cell={cell_name} tool={tool} "
                 f"tool_time={tool_time}s piece_type={piece_type}")
            
            # Record tool usage if tool was used (not IDLE)
            if tool != ETool.IDLE and tool_time > 0 and cell in (100, 200, 300, 400):
                tool_name = f"T{tool}"
                if cell_name not in self._cell_tool_usage:
                    self._cell_tool_usage[cell_name] = {}
                if tool_name not in self._cell_tool_usage[cell_name]:
                    self._cell_tool_usage[cell_name][tool_name] = {"time": 0, "pieces": 0}
                
                self._cell_tool_usage[cell_name][tool_name]["time"] += tool_time
                self._cell_tool_usage[cell_name][tool_name]["pieces"] += 1
                
                try:
                    update_tool_usage(
                        machine_id=cell_name,
                        tool_name=tool_name,
                        total_time_s=tool_time,
                        pieces_processed=1
                    )
                    _log(f"[stats] Tool usage wrote DB {cell_name}/{tool_name} +{tool_time}s")
                except Exception as e:
                    _log(f"[stats] Error recording tool usage: {e}")
                
            # Record unload statistics (pieces reaching unload station U=40)
            if cell == ELocation.U:
                self._unload_counts[piece_type] = self._unload_counts.get(piece_type, 0) + 1
                
                # Unload to dock 1 (can be extended to multiple docks)
                try:
                    update_unload_stats(
                        dock_id=1,
                        piece_type=piece_type,
                        count=1
                    )
                    _log(f"[stats] Unload wrote DB dock=1 piece_type={piece_type}")
                except Exception as e:
                    _log(f"[stats] Error recording unload stats: {e}")

    def _recalculate_locked(self):
        for o in self._orders:
            o.calculate_priority(IN_PROGRESS_BOOST)

    def _sort_locked(self):
        """Highest priority first; completed orders sink to the bottom."""
        self._orders.sort(key=lambda o: (
            0 if o.quantity_remaining > 0 else 1,  # active before done
            -o.priority,
        ))

    def _pick_batch_locked(self) -> list:
        """
        Return up to BATCH_SIZE highest-priority orders that still have
        pieces to produce.  Tries to pick from different orders first;
        fills remaining slots from the top order if needed.
        """
        seen: dict[int, int] = {}   # id(order) -> pieces already picked
        batch = []

        for o in self._orders:
            if len(batch) >= BATCH_SIZE:
                break
            already = seen.get(id(o), 0)
            if o.quantity_done + already < o.quantity:
                batch.append(o)
                seen[id(o)] = already + 1

        return batch

    # ── Display ───────────────────────────────────────────────────────────────

    def _print_queue(self):
        with self._lock:
            active = [o for o in self._orders if o.quantity_remaining > 0]
            done   = [o for o in self._orders if o.quantity_remaining == 0]

        if not active and not done:
            return

        ts = time.strftime("%H:%M:%S")
        print(f"\n  [{ts}]  today={self._pieces_today}/{WAREHOUSE_CAP}  "
              f"active={len(active)}  done={len(done)}")
        print(f"  {'#':>4}  {'Type':4}  {'Done/Qty':>8}  "
              f"{'Score':>9}  {'Penalty':>8}  {'DDate':>5}  Status")
        print(f"  {'─'*4}  {'─'*4}  {'─'*8}  {'─'*9}  "
              f"{'─'*8}  {'─'*5}  {'─'*11}")
        for i, o in enumerate(active[:12]):
            marker = "►" if i == 0 else " "
            print(f"  {marker}{o.db_order_id or 0:3d}  "
                  f"{o.piece_type:4}  "
                  f"{o.quantity_done:3d}/{o.quantity:<3d}  "
                  f"{o.priority:9.3f}  "
                  f"{o.penalty:8}  "
                  f"{o.ddate_days:5d}  "
                  f"{o.status}")
        if done:
            print(f"  ── {len(done)} completed ──")

    def print_stats(self):
        with self._lock:
            orders = list(self._orders)

        uptime  = int(time.time() - self._start_ts)
        n_done  = sum(1 for o in orders if o.quantity_remaining == 0)
        n_pend  = sum(1 for o in orders if o.quantity_remaining > 0)
        p_done  = sum(o.quantity_done for o in orders)
        p_total = sum(o.quantity      for o in orders)

        print(f"\n{'='*55}")
        print(f"  MES Statistics")
        print(f"{'='*55}")
        print(f"  Uptime       : "
              f"{uptime//3600}h {uptime%3600//60}m {uptime%60}s")
        print(f"  Orders       : {len(orders)}  "
              f"({n_done} done, {n_pend} pending)")
        print(f"  Pieces       : {p_done}/{p_total}")
        print(f"  PLC dispatch : {self._dispatched}  "
              f"failed={self._failed}")
        print(f"  Unload cycles: {self._day_cycles}")
        print(f"  Today        : {self._pieces_today}/{WAREHOUSE_CAP}")
        if orders:
            print()
            self._print_queue()
        print()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _log(msg: str):
    ts = time.strftime("%H:%M:%S")
    print(f"  {ts}  {msg}")
    logger.info(msg)


def _banner(msg: str):
    print(f"\n  {'─'*60}")
    print(f"  {msg}")
    print(f"  {'─'*60}")
