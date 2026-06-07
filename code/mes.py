"""
mes.py
======
In-memory MES. No database -- persistence is handled externally.
"""

import logging
import threading
import time
from typing import Optional

from orders import (
    ActiveOrder, ClientOrder, Order as ProdOrder,
    VALID_TYPES, ESTIMATED_TIME,
)

logger = logging.getLogger(__name__)

IN_PROGRESS_BOOST = 1.5
WAREHOUSE_CAP     = 20
UNLOAD_INTERVAL   = 60.0
SCHEDULER_POLL    = 1.5
PRODUCTION_POLL   = 1.0
PRODUCTION_TIMEOUT= 300.0
BATCH_SIZE        = 3
W1_THRESHOLD      = 20
ORDER_HOST        = "0.0.0.0"
ORDER_PORT        = 6666

_ao_id_counter = 0
_ao_id_lock    = threading.Lock()

def _next_ao_id() -> int:
    global _ao_id_counter
    with _ao_id_lock:
        _ao_id_counter += 1
        return _ao_id_counter


class MES:
    def __init__(self, plc=None,
                 order_host: str = ORDER_HOST,
                 order_port: int = ORDER_PORT):
        self._plc        = plc
        self._lock       = threading.Lock()
        self._orders: list[ActiveOrder] = []
        self._pieces_today: int = 0
        self._stop       = threading.Event()
        self._start_ts   = time.time()
        self._order_host = order_host
        self._order_port = order_port
        self._receiver   = None
        self._dispatched = 0
        self._failed     = 0
        self._day_cycles = 0
        self._last_tool_times = {}   # machine_index -> [t0,t1,t2]
        self._MACHINE_NAMES = ["M1a", "M1b", "M2a", "M2b", "M3a", "M3b", "M4a", "M4b"]
        threading.Thread(target=self._unload_loop, daemon=True,
                         name="unload-timer").start()
        self._reload_orders_from_db()

    def _reload_orders_from_db(self):
        """Restore PENDING/IN_PROGRESS orders that survived a restart."""
        try:
            from db_handler import load_active_orders
            rows = load_active_orders()
            if not rows:
                return
            now = time.time()
            with self._lock:
                for row in rows:
                    ao = ActiveOrder(
                        client_order_id = row["external_order_id"],
                        piece_type      = row["type"],
                        quantity        = row["quantity"],
                        quantity_done   = row["quantity_done"],
                        ddate_days      = row["ddate"],
                        penalty         = row["penalty"],
                        status          = row["status"],
                        started_at      = (
                            row["created_at"].timestamp()
                            if hasattr(row["created_at"], "timestamp")
                            else now
                        ),
                    )
                    ao.db_order_id = row["order_id"]
                    ao.calculate_priority(IN_PROGRESS_BOOST)
                    self._orders.append(ao)
                self._sort_locked()
            _log(f"[db] Reloaded {len(rows)} order(s) from DB")
        except Exception as _e:
            _log(f"[db] Order reload failed: {_e}")

    def on_client_order(self, client_order: ClientOrder):
        now = time.time()
        added = []
        with self._lock:
            for o in client_order.orders:
                ao = ActiveOrder(
                    client_order_id = client_order.OrderID,
                    piece_type      = o.type.upper(),
                    quantity        = o.quantity,
                    ddate_days      = o.DDate,
                    penalty         = o.Penalty,
                    status          = "PENDING",
                    started_at      = now,
                )
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
        try:
            from db_handler import save_to_db
            save_to_db(client_order)
        except Exception as _e:
            _log(f"[db] save_to_db failed: {_e}")

    def add_materials(self, wood: int = 0, metal: int = 0):
        _log(f"[materials] add_materials called: wood={wood} metal={metal}  "
             f"-- please load manually via SFS loading docks")

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

    def run(self):
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
                _log(f"[scheduler] Day cap ({self._pieces_today}/{WAREHOUSE_CAP}) -- waiting for unload")
                time.sleep(SCHEDULER_POLL)
                continue
            try:
                w1 = self._plc.get_warehouse_status()["W1"]
                if w1 >= W1_THRESHOLD:
                    _log(f"[scheduler] W1 near capacity ({w1}/{W1_THRESHOLD}) -- waiting")
                    time.sleep(SCHEDULER_POLL)
                    continue
            except Exception as exc:
                _log(f"[scheduler] W1 read error: {exc}")
            self._print_queue()
            types_str = " + ".join(o.piece_type for o in batch_orders)
            _log(f"[scheduler] Dispatching batch of {len(batch_orders)}: {types_str}")
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
                        else:
                            order.status = "IN_PROGRESS"
                            _log(f"[scheduler] ✓ #{order.db_order_id} "
                                 f"{order.quantity_done}/{order.quantity}  "
                                 f"today={self._pieces_today}/{WAREHOUSE_CAP}")
                    else:
                        self._failed += 1
                        order.status = "PENDING"
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

    def _unload_loop(self):
        time.sleep(UNLOAD_INTERVAL)
        while not self._stop.is_set():
            self._do_unload()
            time.sleep(UNLOAD_INTERVAL)

    def _do_unload(self):
        with self._lock:
            count = self._pieces_today
        self._day_cycles += 1
        _log(f"[unload] Day #{self._day_cycles} end -- {count} piece(s) -- resetting")
        with self._lock:
            self._pieces_today = 0
        self._snapshot_machine_stats_to_db()

    def _snapshot_machine_stats_to_db(self):
        """Snapshot PLC machine statistics to DB once per unload cycle."""
        if self._plc is None:
            return
        try:
            from db_handler import snapshot_machine_stats, record_tool_usage, _MACHINE_TOOLS # <-- Přidán import _MACHINE_TOOLS
            machines = self._plc.get_machine_statistics()
            for m in machines:
                i = m["machine_index"]
                if i >= len(self._MACHINE_NAMES):
                    continue
                if m["operating_time"] == 0 and m["pieces_total"] == 0:
                    continue
                
                machine_name = self._MACHINE_NAMES[i]
                
                snapshot_machine_stats(
                    machine_name    = machine_name,
                    total_op_time_s = m["operating_time"],
                    occupation_pct  = m["occupation_pct"],
                    tool_changes    = m["tool_changes"],
                    pieces_total    = m["pieces_total"],
                )
                
                # Získání správných nástrojů pro aktuální stroj (např. ['T8', 'T9', 'T11'])
                available_tools = _MACHINE_TOOLS[machine_name][1]
                
                prev = self._last_tool_times.get(i, [0.0, 0.0, 0.0])
                for j, t in enumerate(m["tool_times"]):
                    delta = t - prev[j]
                    if delta > 0:
                        # Zde použijeme skutečný název z pole místo f"T{j+1}"
                        actual_tool_name = available_tools[j] 
                        record_tool_usage(
                            machine_name, actual_tool_name, delta, 0)
                self._last_tool_times[i] = list(m["tool_times"])
        except Exception as _e:
            _log(f"[db] Machine stats snapshot failed: {_e}")

    def _dispatch_batch_and_wait(self, orders: list) -> bool:
        from opcua_handler import build_recipe
        all_slots = []
        for idx, order in enumerate(orders):
            id_rec, id_proc, id_piece, id_final = self._plc._alloc_ids()
            slots = build_recipe(
                piece_type         = order.piece_type,
                id_recipe          = id_rec,
                id_procedure_start = id_proc,
                id_piece_start     = id_piece,
                id_final_piece     = id_final,
                slot_offset        = idx * 13,   # each recipe occupies its own 13 slots
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
        _log(f"[dispatch] PLC ack -- {n_slots} slots ({len(orders)} piece(s)) -- polling...")
        return self._wait_for_completion(timeout=PRODUCTION_TIMEOUT, max_slots=n_slots + 5)

    def _wait_for_completion(self, timeout: float, max_slots: int = 15) -> bool:
        time.sleep(2.0)
        start      = time.time()
        prev_count = -1
        saw_active = False
        while (time.time() - start) < timeout:
            if self._stop.is_set():
                return False
            try:
                procs   = self._plc._handler.read_procedures(max_slots=max_slots)
                errors  = self._plc.get_errors()
                n       = len(procs)
                elapsed = int(time.time() - start)
                for e in errors:
                    _log(f"[production] PLC error code={e['code']} proc={e['procedure_id']}")
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
        _log(f"[production] Timeout after {timeout:.0f}s (saw_active={saw_active})")
        return saw_active

    def _recalculate_locked(self):
        for o in self._orders:
            o.calculate_priority(IN_PROGRESS_BOOST)

    def _sort_locked(self):
        self._orders.sort(key=lambda o: (
            0 if o.quantity_remaining > 0 else 1,
            -o.priority,
        ))

    def _pick_batch_locked(self) -> list:
        seen: dict[int, int] = {}
        batch = []
        for o in self._orders:
            if len(batch) >= BATCH_SIZE:
                break
            already = seen.get(id(o), 0)
            if o.quantity_done + already < o.quantity:
                batch.append(o)
                seen[id(o)] = already + 1
        return batch

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
        print(f"\n{'='*60}")
        print(f"  MES Statistics")
        print(f"{'='*60}")
        print(f"  Uptime       : {uptime//3600}h {uptime%3600//60}m {uptime%60}s")
        print(f"  Orders       : {len(orders)}  ({n_done} done, {n_pend} pending)")
        print(f"  Pieces       : {p_done}/{p_total}")
        print(f"  PLC dispatch : {self._dispatched}  failed={self._failed}")
        print(f"  Unload cycles: {self._day_cycles}")
        print(f"  Today        : {self._pieces_today}/{WAREHOUSE_CAP}")
        if orders:
            print()
            self._print_queue()

        # ── PLC machine statistics ────────────────────────────────────────────
        if self._plc is not None:
            print("Inside MES.print_stats: fetching PLC machine statistics...")
            try:
                print("Inside MES.print_stats: calling plc.get_machine_statistics()...")
                machines = self._plc.get_machine_statistics()
                active   = [m for m in machines
                             if m["operating_time"] > 0 or m["pieces_total"] > 0]
                if active:
                    print(f"\n  PLC Machine Statistics: {len(active)} active machine(s)")
                    print(f"\n  {'─'*58}")
                    print(f"  PLC Machine Statistics")
                    print(f"  {'─'*58}")
                    print(f"  {'Mach':>4}  {'Occ%':>6}  {'Pieces':>6}  "
                          f"{'Changes':>7}  {'T1(s)':>6}  {'T2(s)':>6}  {'T3(s)':>6}")
                    print(f"  {'─'*4}  {'─'*6}  {'─'*6}  "
                          f"{'─'*7}  {'─'*6}  {'─'*6}  {'─'*6}")
                    for m in active:
                        t = m["tool_times"]
                        print(f"  {m['machine_index']:>4}  "
                              f"{m['occupation_pct']:6.1f}  "
                              f"{m['pieces_total']:6}  "
                              f"{m['tool_changes']:7}  "
                              f"{t[0]:6.0f}  {t[1]:6.0f}  {t[2]:6.0f}")
                elif machines:
                    print(f"\n  PLC Machine Statistics: all machines idle")
                else:
                    print(f"\n  PLC Machine Statistics: no data returned")
            except Exception as _e:
                print(f"\n  PLC Machine Statistics: unavailable ({_e})")
        print()


def _log(msg: str):
    ts = time.strftime("%H:%M:%S")
    print(f"  {ts}  {msg}")
    logger.info(msg)


def _banner(msg: str):
    print(f"\n  {'─'*60}")
    print(f"  {msg}")
    print(f"  {'─'*60}")