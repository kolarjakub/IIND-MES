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
            with self._lock:
                pending = [o for o in self._active_orders_list
                           if o.status == "PENDING"]

                if not pending:
                    continue

                # Recalculate priorities and sort
                for o in pending:
                    o.calculate_priority(in_progress_boost=1.5)
                pending.sort(key=lambda o: o.priority, reverse=True)

                # Try to dispatch highest priority dispatchable order
                dispatched = False
                for order in pending:
                    if self._can_dispatch(order):
                        self._dispatch(order)
                        dispatched = True
                        break
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

                if not dispatched and pending:
                    print(f"{Fore.YELLOW}[MES] No orders dispatchable "
                          f"— waiting for materials{Style.RESET_ALL}")

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
        """Read warehouse counts from PLC on startup."""
        try:
            status   = self._plc.get_warehouse_status()
            total_w1 = status["W1"]
            total_w2 = status["W2"]

            if total_w1 == 0:
                self._warehouse_W1 = ord.WarehouseState(wood=0, metal=0)
            else:
                print(
                    f"{Fore.YELLOW}[MES] W1 has {total_w1} pieces from previous "
                    f"session — material type unknown, assuming empty"
                    f"{Style.RESET_ALL}"
                )
                self._warehouse_W1 = ord.WarehouseState(wood=0, metal=0)

            if total_w2 == 0:
                self._warehouse_W2 = ord.WarehouseState(wood=0, metal=0)
            else:
                print(
                    f"{Fore.YELLOW}[MES] W2 has {total_w2} pieces from previous "
                    f"session{Style.RESET_ALL}"
                )
                self._warehouse_W2 = ord.WarehouseState(wood=0, metal=0)

            print(f"{Fore.GREEN}[MES] Warehouse synced: "
                  f"W1={total_w1} W2={total_w2}{Style.RESET_ALL}")

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

        return {
            "warehouse_W1": {"wood": self._warehouse_W1.wood,
                             "metal": self._warehouse_W1.metal},
            "warehouse_W2": {"wood": self._warehouse_W2.wood,
                             "metal": self._warehouse_W2.metal},
            "pending":      pending,
            "in_progress":  in_progress,
            "completed":    completed,
            "plc_ready":    self._plc.is_ready(),
        }

    # ── Scheduling helpers ────────────────────────────────────────────────────

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
        """Poll PLC for completed procedures and update order statuses."""
        try:
            procedures = self._plc.get_procedures()

            with self._lock:
                in_progress = [o for o in self._active_orders_list
                               if o.status == "IN_PROGRESS"]

                if in_progress:
                    if not procedures:
                        # PLC idle — all IN_PROGRESS orders are done
                        print(f"{Fore.GREEN}[MES] PLC idle — "
                              f"marking {len(in_progress)} order(s) complete"
                              f"{Style.RESET_ALL}")
                        for order in in_progress:
                            self._complete_order(order)
                    else:
                        print(f"[MES] Waiting for PLC — "
                              f"{len(procedures)} procedure(s) still active")

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


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mes = MES()
    mes.run()