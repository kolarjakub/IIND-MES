from plc_interface import PLCInterface
import db_handler as dbh
import order_receiver as orc
import threading
import orders as ord
import time
from colorama import init, Fore, Style

init(autoreset=True)

# ── Constants ─────────────────────────────────────────────────────────────────

SCHEDULING_INTERVAL = 5.0   # seconds between scheduler ticks
STATUS_INTERVAL     = 2.0   # seconds between PLC status polls
WAREHOUSE_CAPACITY  = 32    # max pieces per warehouse (from spec)

# Which cell each product type uses (confirmed from Final_Test PLC code)
# RWM/SWM removed — not implemented in PLC yet (require multi-cell machining)
CELL_MAP = {
    "RWW": "C1",
    "SWW": "C2",
    "RMM": "C3",
    "SMM": "C4",
}


class MES:
    def __init__(self):
        # PLC interface
        self._plc = PLCInterface()

        # Order receiver — single callback handles DB save + active list
        self._order_receiver = orc.OrderReceiver(
            on_order_received=[self._add_order_to_active_list]
        )

        # Warehouse local tracking
        self._warehouse_W1 = ord.WarehouseState(wood=0, metal=0)
        self._warehouse_W2 = ord.WarehouseState(wood=0, metal=0)

        # Active orders list — PENDING and IN_PROGRESS
        self._active_orders_list = list()

        # Lock to protect shared state from race conditions
        self._lock = threading.Lock()

        # Dock assignment — cycles 1->2->3->4->5->1
        self._next_dock = 1

    # ── Startup ───────────────────────────────────────────────────────────────

    def run(self):
        """Start MES — connect to PLC, sync warehouse, start all threads."""
        # Reload any unfinished orders from DB on startup
        for row in dbh.get_pending_orders():
            active = ord.ActiveOrder(
                client_order_id = 0,
                piece_type      = row['type'],
                quantity        = row['quantity'],
                ddate_days      = row['DDate'],
                penalty         = row['penalty'],
                status          = "PENDING",
                db_order_id     = row['order_id'],
            )
            active.calculate_priority(in_progress_boost=1.5)
            self._active_orders_list.append(active)

        if self._active_orders_list:
            print(f"{Fore.CYAN}[MES] Reloaded "
                  f"{len(self._active_orders_list)} pending order(s) from DB"
                  f"{Style.RESET_ALL}")

        print(f"{Fore.GREEN}[MES] Starting...{Style.RESET_ALL}")

        # Connect to PLC
        if not self._plc.connect():
            print(f"{Fore.RED}[MES] Could not connect to PLC. "
                  f"Is CODESYS running?{Style.RESET_ALL}")

        # Sync warehouse state on startup
        self._sync_warehouse_state()

        # Start threads
        threading.Thread(
            target=self._receive_orders, daemon=True, name="receiver"
        ).start()
        threading.Thread(
            target=self._scheduler_loop, daemon=True, name="scheduler"
        ).start()
        threading.Thread(
            target=self._status_loop, daemon=True, name="status"
        ).start()

        print(f"{Fore.GREEN}[MES] All threads started.{Style.RESET_ALL}")

        # Keep main thread alive
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print(f"\n{Fore.YELLOW}[MES] Shutting down...{Style.RESET_ALL}")
            self._plc.disconnect()

    # ── Threads ───────────────────────────────────────────────────────────────

    def _receive_orders(self):
        """Thread: listen for incoming orders on TCP:6666."""
        self._order_receiver.start_server()
        self._order_receiver.receive_orders()

    def _scheduler_loop(self):
        """Thread: every SCHEDULING_INTERVAL seconds, dispatch highest priority order."""
        while True:
            time.sleep(SCHEDULING_INTERVAL)

            # Reconcile warehouse with PLC before scheduling
            self._reconcile_warehouse()

            with self._lock:
                pending = [o for o in self._active_orders_list
                           if o.status == "PENDING"]

                if not pending:
                    continue

                # Recalculate priorities and sort
                for o in pending:
                    o.calculate_priority(in_progress_boost=1.5)
                pending.sort(key=lambda o: o.priority, reverse=True)

                # Try to dispatch one order per available cell (parallel dispatch)
                # e.g. RWW to C1 AND RMM to C3 in same tick
                dispatched_cells = set()
                dispatched_any   = False

                for order in pending:
                    cell = CELL_MAP.get(order.piece_type)

                    # Skip if we already dispatched to this cell this tick
                    if cell in dispatched_cells:
                        continue

                    # Check if cell has free workstations
                    cell_avail = self._plc.get_cell_availability(cell)
                    if not cell_avail["any_free"]:
                        print(
                            f"{Fore.YELLOW}[MES] Cell {cell} full "
                            f"— skipping {order.piece_type}{Style.RESET_ALL}"
                        )
                        continue

                    if self._can_dispatch(order):
                        self._dispatch(order)
                        dispatched_cells.add(cell)
                        dispatched_any = True
                    else:
                        needed = ord.RAW_MATERIALS[order.piece_type]
                        print(
                            f"{Fore.YELLOW}[MES] Waiting for materials: "
                            f"{order.quantity_remaining}x {order.piece_type} "
                            f"needs Wood={needed.get('Wood', 0) * order.quantity_remaining} "
                            f"Metal={needed.get('Metal', 0) * order.quantity_remaining} "
                            f"| W1 has Wood={self._warehouse_W1.wood} "
                            f"Metal={self._warehouse_W1.metal}"
                            f"{Style.RESET_ALL}"
                        )

                if not dispatched_any and pending:
                    print(f"{Fore.YELLOW}[MES] No orders dispatchable "
                          f"— waiting for materials or free cells{Style.RESET_ALL}")

    def _status_loop(self):
        """Thread: every STATUS_INTERVAL seconds, poll PLC for completed procedures."""
        while True:
            time.sleep(STATUS_INTERVAL)
            self._poll_plc_status()

    # ── Order intake ──────────────────────────────────────────────────────────

    def _add_order_to_active_list(self, client_order):
        """Callback from OrderReceiver — saves to DB and creates ActiveOrders."""
        order_ids = dbh.save_to_db(client_order)
        if not order_ids:
            order_ids = []

        with self._lock:
            for i, order in enumerate(client_order.orders):
                if order.type not in ord.VALID_TYPES:
                    print(f"{Fore.RED}[MES] Unknown type {order.type}, "
                          f"skipping{Style.RESET_ALL}")
                    continue

                db_id = order_ids[i] if i < len(order_ids) else None

                active_order = ord.ActiveOrder(
                    client_order_id = client_order.OrderID,
                    piece_type      = order.type,
                    quantity        = order.quantity,
                    ddate_days      = order.DDate,
                    penalty         = order.Penalty,
                    status          = "PENDING",
                    db_order_id     = db_id,
                )
                active_order.calculate_priority(in_progress_boost=1.5)
                self._active_orders_list.append(active_order)
                print(
                    f"{Fore.GREEN}[MES] Queued {order.quantity}x {order.type} "
                    f"| priority={active_order.priority:.3f} "
                    f"| db_id={db_id}{Style.RESET_ALL}"
                )

    # ── Warehouse sync ────────────────────────────────────────────────────────

    def _sync_warehouse_state(self):
        """Read warehouse counts from PLC on startup — type-aware breakdown."""
        try:
            status     = self._plc.get_warehouse_status()
            total_w1   = status["W1"]
            total_w2   = status["W2"]
            w1_wood    = status.get("W1_wood",     0)
            w1_metal   = status.get("W1_metal",    0)
            w2_finished= status.get("W2_finished", 0)

            self._warehouse_W1 = ord.WarehouseState(wood=w1_wood,    metal=w1_metal)
            self._warehouse_W2 = ord.WarehouseState(wood=w2_finished, metal=0)

            print(
                f"{Fore.GREEN}[MES] Warehouse synced: "
                f"W1={total_w1} (Wood={w1_wood} Metal={w1_metal}) "
                f"W2={total_w2} (Finished={w2_finished})"
                f"{Style.RESET_ALL}"
            )

        except Exception as e:
            print(f"{Fore.RED}[MES] Could not sync warehouse: {e}{Style.RESET_ALL}")

    # ── ERP interface — called directly by ERP ────────────────────────────────

    def add_materials(self, wood: int = 0, metal: int = 0):
        """
        Called by ERP to notify MES that raw materials have been loaded into W1.

        The ERP calls this after purchasing from a supplier and the materials
        arrive at the loading cell L. MES updates its W1 tracking accordingly
        so the scheduler can dispatch pending orders.

        Args:
            wood:  number of Wood pieces added to W1
            metal: number of Metal pieces added to W1
        """
        if wood < 0 or metal < 0:
            print(f"{Fore.RED}[MES] add_materials: negative values not allowed"
                  f"{Style.RESET_ALL}")
            return

        with self._lock:
            self._warehouse_W1.wood  += wood
            self._warehouse_W1.metal += metal

        print(
            f"{Fore.GREEN}[MES] Materials added: +{wood} Wood +{metal} Metal "
            f"| W1 now: Wood={self._warehouse_W1.wood} "
            f"Metal={self._warehouse_W1.metal}{Style.RESET_ALL}"
        )

    def get_status(self) -> dict:
        """
        Called by ERP to get a snapshot of MES state.

        Returns dict with:
            warehouse_W1: {"wood": int, "metal": int}
            warehouse_W2: {"wood": int, "metal": int}
            pending:      number of pending orders
            in_progress:  number of in-progress orders
            completed:    number of completed orders
            plc_ready:    bool
        """
        with self._lock:
            pending     = sum(1 for o in self._active_orders_list if o.status == "PENDING")
            in_progress = sum(1 for o in self._active_orders_list if o.status == "IN_PROGRESS")
            completed   = sum(1 for o in self._active_orders_list if o.status == "COMPLETED")

            pending_orders = [
                {"piece_type": o.piece_type, "quantity": o.quantity_remaining,
                 "client_order_id": o.client_order_id, "status": "PENDING"}
                for o in self._active_orders_list if o.status == "PENDING"
            ]
            in_progress_orders = [
                {"piece_type": o.piece_type, "quantity": o.quantity_remaining,
                 "client_order_id": o.client_order_id, "status": "IN_PROGRESS",
                 "dock": getattr(o, "dock", "?")}
                for o in self._active_orders_list if o.status == "IN_PROGRESS"
            ]

        return {
            "warehouse_W1":       {"wood": self._warehouse_W1.wood,
                                   "metal": self._warehouse_W1.metal},
            "warehouse_W2":       {"wood": self._warehouse_W2.wood,
                                   "metal": self._warehouse_W2.metal},
            "pending":            pending,
            "in_progress":        in_progress,
            "completed":          completed,
            "plc_ready":          self._plc.is_ready(),
            "pending_orders":     pending_orders,
            "in_progress_orders": in_progress_orders,
        }

    # ── Scheduling helpers ────────────────────────────────────────────────────

    def _reconcile_warehouse(self):
        """
        Sync MES warehouse tracking with PLC data.

        Strategy:
        - If PLC has MORE than MES thinks → trust PLC (piece arrived we missed)
        - If PLC has LESS than MES thinks → keep MES value
          (ERP simulated delivery — piece not physically in SFS yet but
           is allocated for production. Reducing would block scheduling.)
        - If PLC has ZERO and MES > 0 → only reset if we have no pending
          orders waiting for material (safety check for complete drift)
        """
        try:
            inv       = self._plc.get_warehouse_status()
            plc_wood  = inv.get("W1_wood",  0)
            plc_metal = inv.get("W1_metal", 0)

            with self._lock:
                mes_wood  = self._warehouse_W1.wood
                mes_metal = self._warehouse_W1.metal

                if plc_wood == mes_wood and plc_metal == mes_metal:
                    return  # in sync

                # Only correct upward — never reduce simulated ERP stock
                new_wood  = max(mes_wood,  plc_wood)
                new_metal = max(mes_metal, plc_metal)

                if new_wood != mes_wood or new_metal != mes_metal:
                    print(
                        f"{Fore.YELLOW}[MES] W1 corrected up: "
                        f"Wood:{mes_wood}→{new_wood} "
                        f"Metal:{mes_metal}→{new_metal}{Style.RESET_ALL}"
                    )
                    self._warehouse_W1 = ord.WarehouseState(
                        wood=new_wood, metal=new_metal
                    )

        except Exception as e:
            print(f"{Fore.RED}[MES] Warehouse reconcile failed: {e}{Style.RESET_ALL}")

    def _can_dispatch(self, order: ord.ActiveOrder) -> bool:
        """Check if warehouse has enough material and W2 has space."""
        needed   = ord.RAW_MATERIALS[order.piece_type]
        wood_ok  = self._warehouse_W1.wood  >= needed.get("Wood",  0) * order.quantity_remaining
        metal_ok = self._warehouse_W1.metal >= needed.get("Metal", 0) * order.quantity_remaining
        w2_space = (self._warehouse_W2.total + order.quantity_remaining) <= WAREHOUSE_CAPACITY
        return wood_ok and metal_ok and w2_space

    def _get_next_dock(self) -> int:
        """Assign next available unloading dock, cycling 1-5."""
        dock = self._next_dock
        self._next_dock = (self._next_dock % 5) + 1
        return dock

    def _dispatch(self, order: ord.ActiveOrder):
        """Send order to PLC with dock assignment and update order status."""
        qty  = order.quantity_remaining
        dock = self._get_next_dock()

        print(f"{Fore.CYAN}[MES] Dispatching {qty}x {order.piece_type} "
              f"-> dock {dock}...{Style.RESET_ALL}")

        # Use unload_order for large quantities (auto-splits across docks)
        # Use create_pieces_for_unload for <= 6 pieces (single dock)
        if qty <= 6:
            success = self._plc.create_pieces_for_unload(
                order.piece_type, qty, dock=dock
            )
        else:
            success = self._plc.unload_order(order.piece_type, qty)

        if success:
            order.status     = "IN_PROGRESS"
            order.started_at = time.time()
            order.dock       = dock
            order.calculate_priority(in_progress_boost=1.5)

            if order.db_order_id is not None:
                dbh.update_order_status(order.db_order_id, "IN_PROGRESS")

            # Deduct materials from W1 tracking
            needed = ord.RAW_MATERIALS[order.piece_type]
            self._warehouse_W1.wood  -= needed.get("Wood",  0) * qty
            self._warehouse_W1.metal -= needed.get("Metal", 0) * qty

            print(
                f"{Fore.GREEN}[MES] Dispatched {qty}x {order.piece_type} "
                f"-> dock {dock} | "
                f"W1 remaining: Wood={self._warehouse_W1.wood} "
                f"Metal={self._warehouse_W1.metal}{Style.RESET_ALL}"
            )
        else:
            print(f"{Fore.RED}[MES] PLC rejected {order.piece_type} "
                  f"— will retry next tick{Style.RESET_ALL}")

    # ── Status polling ────────────────────────────────────────────────────────

    def _poll_plc_status(self):
        """
        Poll PLC for completed procedures and update order statuses.

        Sequence:
          1. Procedures go to 0 — PLC finished processing
          2. Piece travels conveyor to W2 — takes a few more seconds
          3. W2 count increases — piece physically arrived
          4. Mark order complete

        We wait for W2 increase rather than procedures=0 to avoid
        marking complete before the piece actually arrives.
        """
        try:
            procedures = self._plc.get_procedures()
            inv        = self._plc.get_warehouse_status()
            w2_now     = inv.get("W2", 0)

            with self._lock:
                in_progress = [o for o in self._active_orders_list
                               if o.status == "IN_PROGRESS"]

                if not in_progress:
                    return

                if procedures:
                    print(f"[MES] Waiting for PLC — "
                          f"{len(procedures)} procedure(s) still active")
                    return

                # Procedures cleared — now wait for W2 to increase
                # Get total expected pieces from in_progress orders
                expected_pieces = sum(o.quantity for o in in_progress)
                w2_baseline = getattr(self, "_w2_baseline", 0)

                if not hasattr(self, "_procedures_cleared_at"):
                    # First tick with procedures=0 — record W2 baseline
                    self._procedures_cleared_at = True
                    self._w2_baseline = w2_now
                    print(
                        f"{Fore.YELLOW}[MES] Procedures cleared — "
                        f"waiting for piece(s) to arrive in W2 "
                        f"(W2 now={w2_now}){Style.RESET_ALL}"
                    )
                    return

                # Check if W2 increased enough
                new_pieces = w2_now - self._w2_baseline
                if new_pieces >= expected_pieces:
                    print(
                        f"{Fore.GREEN}[MES] Piece(s) arrived in W2 "
                        f"(+{new_pieces}) — marking {len(in_progress)} "
                        f"order(s) complete{Style.RESET_ALL}"
                    )
                    # Reset tracking
                    del self._procedures_cleared_at
                    self._w2_baseline = 0
                    for order in in_progress:
                        self._complete_order(order)
                else:
                    print(
                        f"{Fore.YELLOW}[MES] Procedures done, waiting for W2 "
                        f"(got {new_pieces}/{expected_pieces} pieces){Style.RESET_ALL}"
                    )

        except Exception as e:
            print(f"{Fore.RED}[MES] Status poll failed: {e}{Style.RESET_ALL}")

    def _complete_order(self, order: ord.ActiveOrder):
        """Mark order as completed, update warehouse tracking, update DB."""
        order.status        = "COMPLETED"
        order.quantity_done = order.quantity
        dock = getattr(order, "dock", "?")

        print(
            f"{Fore.GREEN}"
            f"╔══════════════════════════════════════╗\n"
            f"║  ORDER COMPLETED                     ║\n"
            f"║  Type    : {order.piece_type:<26}║\n"
            f"║  Quantity: {order.quantity:<26}║\n"
            f"║  Dock    : {str(dock):<26}║\n"
            f"╚══════════════════════════════════════╝"
            f"{Style.RESET_ALL}"
        )

        if order.db_order_id is not None:
            dbh.update_order_status(order.db_order_id, "COMPLETED")

        # Notify ERP so penalty accrual stops
        if hasattr(self, "_erp") and self._erp is not None:
            self._erp.mark_order_delivered(order.client_order_id, order.piece_type)

        # Update W2 tracking
        self._warehouse_W2.wood = max(0, self._warehouse_W2.wood - order.quantity)

    def set_erp(self, erp):
        """Set reference to ERP instance for order completion callbacks."""
        self._erp = erp


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mes = MES()
    mes.run()