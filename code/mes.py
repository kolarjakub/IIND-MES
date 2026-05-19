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

        # Order receiver — callbacks: MES intake + DB save
        self._order_receiver = orc.OrderReceiver(
            on_order_received=[self._add_order_to_active_list, dbh.save_to_db]
        )

        # Warehouse local tracking
        self._warehouse_W1 = ord.WarehouseState(wood=0, metal=0)
        self._warehouse_W2 = ord.WarehouseState(wood=0, metal=0)

        # Active orders list — PENDING and IN_PROGRESS
        self._active_orders_list = list()

        # Lock to protect active orders list from race conditions
        self._lock = threading.Lock()

    # ── Startup ───────────────────────────────────────────────────────────────

    def run(self):
        """Start MES — connect to PLC, sync warehouse, start all threads."""
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
                # Get all pending orders
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
        """Callback from OrderReceiver — splits ClientOrder into ActiveOrders."""
        with self._lock:
            for order in client_order.orders:
                if order.type not in ord.VALID_TYPES:
                    print(f"{Fore.RED}[MES] Unknown type {order.type}, "
                          f"skipping{Style.RESET_ALL}")
                    continue

                active_order = ord.ActiveOrder(
                    client_order_id = client_order.OrderID,
                    piece_type      = order.type,
                    quantity        = order.quantity,
                    ddate_days      = order.DDate,
                    penalty         = order.Penalty,
                )
                active_order.calculate_priority(in_progress_boost=1.5)
                self._active_orders_list.append(active_order)
                print(
                    f"{Fore.GREEN}[MES] Queued {order.quantity}x {order.type} "
                    f"| priority={active_order.priority:.3f}{Style.RESET_ALL}"
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

    # ── Scheduling helpers ────────────────────────────────────────────────────

    def _can_dispatch(self, order: ord.ActiveOrder) -> bool:
        """Check if warehouse has enough material and W2 has space."""
        needed   = ord.RAW_MATERIALS[order.piece_type]
        wood_ok  = self._warehouse_W1.wood  >= needed.get("Wood",  0) * order.quantity_remaining
        metal_ok = self._warehouse_W1.metal >= needed.get("Metal", 0) * order.quantity_remaining
        w2_space = (self._warehouse_W2.total + order.quantity_remaining) <= WAREHOUSE_CAPACITY
        return wood_ok and metal_ok and w2_space

    def _dispatch(self, order: ord.ActiveOrder):
        """Send order to PLC and update order status."""
        print(f"{Fore.CYAN}[MES] Dispatching {order.quantity_remaining}x "
              f"{order.piece_type}...{Style.RESET_ALL}")

        success = self._plc.create_pieces(
            order.piece_type,
            order.quantity_remaining
        )

        if success:
            order.status     = "IN_PROGRESS"
            order.started_at = time.time()
            order.calculate_priority(in_progress_boost=1.5)

            # Deduct materials from W1 tracking
            needed = ord.RAW_MATERIALS[order.piece_type]
            self._warehouse_W1.wood  -= needed.get("Wood",  0) * order.quantity_remaining
            self._warehouse_W1.metal -= needed.get("Metal", 0) * order.quantity_remaining

            print(
                f"{Fore.GREEN}[MES] Dispatched {order.quantity_remaining}x "
                f"{order.piece_type} | "
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
            if procedures:
                print(f"[MES] Active procedures: {len(procedures)}")
            # TODO: match completed procedures to active orders
            # and call _complete_order() when done
        except Exception as e:
            print(f"{Fore.RED}[MES] Status poll failed: {e}{Style.RESET_ALL}")

    def _complete_order(self, order: ord.ActiveOrder):
        """Mark order as completed, update warehouse tracking, update DB."""
        order.status        = "COMPLETED"
        order.quantity_done = order.quantity

        print(
            f"{Fore.GREEN}[MES] Completed {order.quantity}x "
            f"{order.piece_type}{Style.RESET_ALL}"
        )
        # TODO: update DB
        # dbh.db_update_order_status(order.client_order_id, "COMPLETED")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mes = MES()
    mes.run()