"""
erp.py
======
Enterprise Resource Planning module.

Responsibilities:
  - Receive client orders via TCP:6666
  - Track simulated time (60s = 1 day)
  - Calculate material requirements from pending orders
  - Order materials from suppliers (A or B based on cost/quantity)
  - Track pending deliveries and deliver to MES when due
  - Track penalties for overdue orders
  - End-of-day: trigger dock flush via MES

Suppliers (from spec):
  SupplierA: Wood  min=2,  price=€10/piece, lead=0 days
  SupplierA: Metal min=4,  price=€15/piece, lead=0 days
  SupplierB: Wood  min=12, price=€2/piece,  lead=2 days
  SupplierB: Metal min=8,  price=€4/piece,  lead=4 days

Usage (from main.py):
    from mes import MES
    from erp import ERP

    mes = MES()
    erp = ERP(mes)

    mes_thread = threading.Thread(target=mes.run, daemon=True)
    mes_thread.start()

    time.sleep(3)  # let MES connect to PLC

    erp.run()  # blocks — handles orders + day ticks
"""

import threading
import time
from dataclasses import dataclass, field
from colorama import init, Fore, Style
import db_handler as dbh
from order_receiver import OrderReceiver
from orders import ClientOrder, Order, VALID_TYPES, RAW_MATERIALS

init(autoreset=True)

# ── Simulation constants ──────────────────────────────────────────────────────

DAY_DURATION_S = 60.0   # seconds per simulated day

# ── Supplier definitions ──────────────────────────────────────────────────────

SUPPLIERS = {
    "A": {
        "Wood":  {"min_order": 2,  "price": 10.0, "lead_days": 0},
        "Metal": {"min_order": 4,  "price": 15.0, "lead_days": 0},
    },
    "B": {
        "Wood":  {"min_order": 12, "price": 2.0,  "lead_days": 2},
        "Metal": {"min_order": 8,  "price": 4.0,  "lead_days": 4},
    },
}

# W1 stock levels — keep W1 stocked but never overflow
# W1 capacity = 32, leave ~8 slots free for internal movement
W1_TARGET_WOOD  = 12   # target per material type
W1_TARGET_METAL = 12
W1_MAX_CAPACITY = 26   # never order above this total (32 - 6 buffer)


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class SupplierDelivery:
    """A pending material delivery from a supplier."""
    supplier:     str    # "A" or "B"
    material:     str    # "Wood" or "Metal"
    quantity:     int
    cost:         float
    ordered_day:  int
    delivery_day: int    # day on which materials arrive


@dataclass
class PenaltyRecord:
    """Tracks penalty accrual for a client order."""
    client_order_id: int
    piece_type:      str
    quantity:        int
    ddate_day:       int    # absolute day deadline
    penalty_per_day: float
    total_penalty:   float = 0.0
    delivered:       bool  = False


# ── ERP class ─────────────────────────────────────────────────────────────────

class ERP:

    def __init__(self, mes):
        """
        Args:
            mes: running MES instance — ERP calls mes.add_materials(),
                 mes.get_status(), mes.on_day_end()
        """
        self._mes = mes

        # Time tracking
        self._current_day  = 1
        self._day_start    = time.time()
        self._lock         = threading.Lock()

        # Supplier deliveries in transit
        self._pending_deliveries: list[SupplierDelivery] = []

        # Penalty tracking
        self._penalty_records: list[PenaltyRecord] = []

        # Total costs accumulated
        self._total_material_cost = 0.0
        self._total_penalties     = 0.0

        # Order receiver — ERP receives client orders on TCP:6666
        self._order_receiver = OrderReceiver(
            on_order_received=[self._handle_client_order]
        )

    # ── Main entry point ──────────────────────────────────────────────────────

    def run(self):
        """Start ERP — starts TCP listener + day clock. Blocks."""
        print(f"{Fore.GREEN}[ERP] Starting — day {self._current_day}{Style.RESET_ALL}")
        print(f"{Fore.GREEN}[ERP] 1 day = {DAY_DURATION_S}s{Style.RESET_ALL}")

        # Start order receiver thread
        threading.Thread(
            target=self._receive_orders, daemon=True, name="erp-receiver"
        ).start()

        # Start day clock thread
        threading.Thread(
            target=self._day_clock_loop, daemon=True, name="erp-clock"
        ).start()

        print(f"{Fore.GREEN}[ERP] Running. Listening for orders on TCP:6666{Style.RESET_ALL}")

        # Start live dashboard thread
        threading.Thread(
            target=self._dashboard_loop, daemon=True, name="erp-dashboard"
        ).start()

        # Keep alive
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print(f"\n{Fore.YELLOW}[ERP] Shutting down...{Style.RESET_ALL}")
            self._print_summary()

    # ── TCP order receiver ────────────────────────────────────────────────────

    def _receive_orders(self):
        """Thread: listen for client orders on TCP:6666."""
        self._order_receiver.start_server()
        self._order_receiver.receive_orders()

    def _handle_client_order(self, client_order: ClientOrder):
        """
        Called when a client order arrives via TCP.
        Registers penalty tracking and forwards to MES.
        """
        print(f"{Fore.CYAN}[ERP] Order received from {client_order.name} "
              f"(ID={client_order.OrderID}){Style.RESET_ALL}")

        with self._lock:
            for order in client_order.orders:
                if order.type not in VALID_TYPES:
                    print(f"{Fore.RED}[ERP] Unknown type {order.type}, "
                          f"skipping{Style.RESET_ALL}")
                    continue

                # Register penalty tracking
                deadline_day = self._current_day + order.DDate
                record = PenaltyRecord(
                    client_order_id = client_order.OrderID,
                    piece_type      = order.type,
                    quantity        = order.quantity,
                    ddate_day       = deadline_day,
                    penalty_per_day = order.Penalty,
                )
                self._penalty_records.append(record)

                print(
                    f"{Fore.CYAN}[ERP] {order.quantity}x {order.type} "
                    f"due day {deadline_day} "
                    f"(penalty €{order.Penalty}/day){Style.RESET_ALL}"
                )

        # Forward to MES
        self._mes._add_order_to_active_list(client_order)

    # ── Day clock ─────────────────────────────────────────────────────────────

    def _day_clock_loop(self):
        """Thread: fires day-end logic every DAY_DURATION_S seconds."""
        while True:
            time.sleep(DAY_DURATION_S)
            self._on_day_end()

    def _on_day_end(self):
        """Called at the end of each simulated day."""
        with self._lock:
            self._current_day += 1
            day = self._current_day

        print(f"\n{Fore.CYAN}{'='*50}{Style.RESET_ALL}")
        print(f"{Fore.CYAN}[ERP] *** DAY {day} BEGINS ***{Style.RESET_ALL}")
        print(f"{Fore.CYAN}{'='*50}{Style.RESET_ALL}")

        # 1. Process deliveries due today
        self._process_deliveries(day)

        # 2. Calculate material needs and order from suppliers
        self._restock_materials(day)

        # 3. Check penalties
        self._check_penalties(day)

        # 4. Print daily status
        self._print_daily_status(day)

    # ── Supplier management ───────────────────────────────────────────────────

    def _choose_supplier(self, material: str, quantity: int,
                         urgent: bool = False) -> str:
        """
        Choose best supplier for a given material and quantity.

        urgent=True  → always use SupplierA (0 day lead) even if more expensive.
                       Used when pending orders are already waiting for material.
        urgent=False → cost optimisation: use SupplierB if cheaper and quantity
                       meets minimum order.

        Returns supplier name "A" or "B".
        """
        if urgent:
            # Orders are waiting — pay more, get it now
            print(f"{Fore.YELLOW}[ERP] Urgent order for {material} "
                  f"— using SupplierA (0 day lead){Style.RESET_ALL}")
            return "A"

        sup_a = SUPPLIERS["A"][material]
        sup_b = SUPPLIERS["B"][material]

        cost_a = quantity * sup_a["price"]
        # Round up to minimum order for B
        qty_b  = max(quantity, sup_b["min_order"])
        cost_b = qty_b * sup_b["price"]

        # Use B only if cheaper AND quantity meets minimum
        if cost_b < cost_a and quantity >= sup_b["min_order"]:
            return "B"
        else:
            return "A"

    def _place_order(self, material: str, quantity: int, current_day: int,
                     urgent: bool = False):
        """
        Place a material order with the best supplier.
        urgent=True forces SupplierA for immediate delivery.
        Records the delivery for future processing.
        """
        supplier = self._choose_supplier(material, quantity, urgent=urgent)
        sup_info = SUPPLIERS[supplier][material]

        # Respect minimum order quantity
        actual_qty = max(quantity, sup_info["min_order"])
        cost       = actual_qty * sup_info["price"]
        delivery_day = current_day + sup_info["lead_days"]

        delivery = SupplierDelivery(
            supplier     = supplier,
            material     = material,
            quantity     = actual_qty,
            cost         = cost,
            ordered_day  = current_day,
            delivery_day = delivery_day,
        )

        with self._lock:
            self._pending_deliveries.append(delivery)
            self._total_material_cost += cost

        print(
            f"{Fore.GREEN}[ERP] Ordered {actual_qty}x {material} "
            f"from Supplier{supplier} "
            f"(€{cost:.2f}, arrives day {delivery_day}){Style.RESET_ALL}"
        )

        # Log to DB
        # dbh.record_supplier_order(supplier, material, actual_qty, cost, delivery_day)

    def _process_deliveries(self, current_day: int):
        """Check pending deliveries — deliver anything due today."""
        with self._lock:
            due      = [d for d in self._pending_deliveries
                        if d.delivery_day <= current_day]
            remaining = [d for d in self._pending_deliveries
                         if d.delivery_day > current_day]
            self._pending_deliveries = remaining

        for delivery in due:
            print(
                f"{Fore.GREEN}[ERP] Delivery arrived: "
                f"{delivery.quantity}x {delivery.material} "
                f"from Supplier{delivery.supplier}{Style.RESET_ALL}"
            )
            if delivery.material == "Wood":
                self._mes.add_materials(wood=delivery.quantity, metal=0)
            else:
                self._mes.add_materials(wood=0, metal=delivery.quantity)

    def _restock_materials(self, current_day: int):
        """
        Check W1 levels and order materials if below target.
        Respects W1 capacity — never orders more than free space allows.
        Leaves buffer slots free for internal piece movement.
        """
        status = self._mes.get_status()
        w1     = status["warehouse_W1"]

        current_wood  = w1["wood"]
        current_metal = w1["metal"]

        # Add in-transit quantities
        with self._lock:
            in_transit_wood  = sum(d.quantity for d in self._pending_deliveries
                                   if d.material == "Wood")
            in_transit_metal = sum(d.quantity for d in self._pending_deliveries
                                   if d.material == "Metal")

        effective_wood  = current_wood  + in_transit_wood
        effective_metal = current_metal + in_transit_metal
        effective_total = effective_wood + effective_metal

        # Check if W1 is nearly full — hold orders if so
        if effective_total >= W1_MAX_CAPACITY:
            print(f"{Fore.YELLOW}[ERP] W1 nearly full "
                  f"(Wood={current_wood}+{in_transit_wood} transit, "
                  f"Metal={current_metal}+{in_transit_metal} transit, "
                  f"total effective={effective_total}/{W1_MAX_CAPACITY}) "
                  f"— holding material orders{Style.RESET_ALL}")
            return

        # Free space available for new orders
        free_space = W1_MAX_CAPACITY - effective_total

        # Calculate what's needed for pending orders
        pending_orders = self._get_pending_material_needs()
        needed_wood    = pending_orders.get("Wood",  0)
        needed_metal   = pending_orders.get("Metal", 0)

        # How much to order — target level OR what pending orders need
        want_wood  = max(0, W1_TARGET_WOOD  - effective_wood,
                         needed_wood - effective_wood)
        want_metal = max(0, W1_TARGET_METAL - effective_metal,
                         needed_metal - effective_metal)

        # Cap by available free space (split evenly if both needed)
        if want_wood > 0 and want_metal > 0:
            order_wood  = min(want_wood,  free_space // 2)
            order_metal = min(want_metal, free_space // 2)
        else:
            order_wood  = min(want_wood,  free_space)
            order_metal = min(want_metal, free_space)

        # Urgent if pending orders are already waiting for this material
        wood_urgent  = needed_wood  > effective_wood
        metal_urgent = needed_metal > effective_metal

        if order_wood > 0:
            print(f"{Fore.YELLOW}[ERP] Ordering Wood: "
                  f"have {current_wood}+{in_transit_wood} transit, "
                  f"need {needed_wood}, free space={free_space} "
                  f"→ ordering {order_wood} "
                  f"({'URGENT' if wood_urgent else 'restock'}){Style.RESET_ALL}")
            self._place_order("Wood", order_wood, current_day, urgent=wood_urgent)

        if order_metal > 0:
            print(f"{Fore.YELLOW}[ERP] Ordering Metal: "
                  f"have {current_metal}+{in_transit_metal} transit, "
                  f"need {needed_metal}, free space={free_space} "
                  f"→ ordering {order_metal} "
                  f"({'URGENT' if metal_urgent else 'restock'}){Style.RESET_ALL}")
            self._place_order("Metal", order_metal, current_day, urgent=metal_urgent)

        if order_wood == 0 and order_metal == 0:
            print(f"{Fore.GREEN}[ERP] W1 stock OK "
                  f"(Wood={current_wood}+{in_transit_wood} transit, "
                  f"Metal={current_metal}+{in_transit_metal} transit)"
                  f"{Style.RESET_ALL}")

    def _get_pending_material_needs(self) -> dict:
        """Calculate total materials needed for all PENDING MES orders."""
        status = self._mes.get_status()
        wood, metal = 0, 0
        for order in status.get("pending_orders", []):
            needed = RAW_MATERIALS.get(order["piece_type"], {})
            wood  += needed.get("Wood",  0) * order["quantity"]
            metal += needed.get("Metal", 0) * order["quantity"]
        return {"Wood": wood, "Metal": metal}

    # ── Penalty tracking ──────────────────────────────────────────────────────

    def _check_penalties(self, current_day: int):
        """Check all orders for overdue penalties."""
        with self._lock:
            records = list(self._penalty_records)

        overdue = []
        for record in records:
            if record.delivered:
                continue
            if current_day > record.ddate_day:
                days_late = current_day - record.ddate_day
                penalty   = record.penalty_per_day * days_late
                record.total_penalty = penalty
                self._total_penalties += record.penalty_per_day
                overdue.append(record)

        if overdue:
            print(f"{Fore.RED}[ERP] ⚠ OVERDUE ORDERS:{Style.RESET_ALL}")
            for r in overdue:
                days_late = current_day - r.ddate_day
                print(
                    f"{Fore.RED}  Order {r.client_order_id}: "
                    f"{r.quantity}x {r.piece_type} "
                    f"{days_late} day(s) late — "
                    f"penalty €{r.total_penalty:.2f}{Style.RESET_ALL}"
                )

    def mark_order_delivered(self, client_order_id: int, piece_type: str):
        """
        Called when MES completes an order — stops penalty accrual.
        ERP records the delivery.
        """
        with self._lock:
            for record in self._penalty_records:
                if (record.client_order_id == client_order_id and
                        record.piece_type == piece_type and
                        not record.delivered):
                    record.delivered = True
                    if record.total_penalty > 0:
                        print(
                            f"{Fore.YELLOW}[ERP] Order {client_order_id} "
                            f"{piece_type} delivered — "
                            f"total penalty €{record.total_penalty:.2f}"
                            f"{Style.RESET_ALL}"
                        )
                    else:
                        print(
                            f"{Fore.GREEN}[ERP] Order {client_order_id} "
                            f"{piece_type} delivered on time — no penalty"
                            f"{Style.RESET_ALL}"
                        )
                    break

    # ── Live dashboard ────────────────────────────────────────────────────────

    def _dashboard_loop(self):
        """Thread: print live status every 15 seconds."""
        time.sleep(5)  # wait for initial startup noise to settle
        while True:
            self._print_live_status()
            time.sleep(15)

    def _print_live_status(self):
        """Print a live dashboard showing current system state."""
        status = self._mes.get_status()
        w1     = status["warehouse_W1"]
        w2     = status["warehouse_W2"]

        with self._lock:
            day          = self._current_day
            secs_left    = max(0, DAY_DURATION_S - (time.time() - self._day_start))
            pending_recs = [r for r in self._penalty_records if not r.delivered]
            in_transit   = list(self._pending_deliveries)

        # Cell activity from procedures
        procedures = self._mes._plc.get_procedures()

        C = "[96m"   # cyan header
        G = Fore.GREEN
        Y = Fore.YELLOW
        R = Fore.RED
        W = Fore.WHITE
        X = Style.RESET_ALL

        print(f"\n{C}{'─'*56}{X}")
        print(f"{C}  LIVE DASHBOARD  —  Day {day}  "
              f"({secs_left:.0f}s until next day){X}")
        print(f"{C}{'─'*56}{X}")

        # Warehouse
        # Get extended status for WIP info
        try:
            raw_status = self._mes._plc.get_warehouse_status()
            w2_finished = raw_status.get("W2_finished", 0)
        except Exception:
            w1_wip = w2_finished = 0

        print(f"{W}  Warehouse W1 : {G}Wood={w1['wood']:3d}  Metal={w1['metal']:3d}{X}")
        print(f"{W}  Warehouse W2 : {G}Finished={w2_finished:3d} tables{X}")

        # PLC activity
        print(f"{W}  PLC active   : {G if not procedures else Y}"
              f"{len(procedures)} procedure(s) running{X}")

        # Orders — show all MES orders (pending + in progress)
        print(f"{C}  ── Orders ──────────────────────────────────{X}")

        mes_pending    = status.get("pending_orders", [])
        mes_in_progress = status.get("in_progress_orders", [])
        all_mes_orders  = mes_pending + mes_in_progress

        if not all_mes_orders and not pending_recs:
            print(f"    {G}No active orders{X}")
        else:
            # Show MES orders with ERP penalty info where available
            shown = set()
            for o in all_mes_orders:
                key = (o.get("client_order_id", 0), o["piece_type"])
                if key in shown:
                    continue
                shown.add(key)

                mes_status = o.get("status", "PENDING")
                status_color = Y if mes_status == "IN_PROGRESS" else W

                # Find matching penalty record
                penalty_str = ""
                for r in pending_recs:
                    if r.piece_type == o["piece_type"] and not r.delivered:
                        days_left = r.ddate_day - day
                        if days_left < 0:
                            penalty_str = f" {R}OVERDUE {abs(days_left)}d  €{r.total_penalty:.0f}{X}"
                        elif days_left == 0:
                            penalty_str = f" {R}DUE TODAY{X}"
                        elif days_left <= 2:
                            penalty_str = f" {Y}due in {days_left}d  €{r.penalty_per_day}/d{X}"
                        else:
                            penalty_str = f" {G}due in {days_left}d  €{r.penalty_per_day}/d{X}"
                        break

                print(f"    {status_color}[{mes_status:11s}]{X} "
                      f"{o['quantity']}x {o['piece_type']:4s}"
                      f"{penalty_str}")

            # Also show ERP-only records not in MES list
            for r in pending_recs:
                if r.delivered:
                    continue
                found = any(o["piece_type"] == r.piece_type for o in all_mes_orders)
                if not found:
                    days_left = r.ddate_day - day
                    if days_left < 0:
                        s = f"{R}OVERDUE {abs(days_left)}d  €{r.total_penalty:.0f}{X}"
                    else:
                        s = f"{G}due in {days_left}d{X}"
                    print(f"    {Y}[ERP ONLY  ]{X} "
                          f"{r.quantity}x {r.piece_type:4s} {s}")

        # Pending deliveries
        if in_transit:
            print(f"{C}  ── Incoming Deliveries ─────────────────────{X}")
            for d in in_transit:
                days_until = d.delivery_day - day
                print(f"    Supplier{d.supplier}: "
                      f"{d.quantity}x {d.material:<6} "
                      f"arrives day {d.delivery_day} "
                      f"({days_until}d)  €{d.cost:.2f}")

        # Financials
        print(f"{C}  ── Financials ──────────────────────────────{X}")
        print(f"    Material cost : €{self._total_material_cost:.2f}")
        print(f"    Penalties     : {R if self._total_penalties > 0 else G}"
              f"€{self._total_penalties:.2f}{X}")
        print(f"    Net cost      : €{self._total_material_cost + self._total_penalties:.2f}")
        print(f"{C}{'─'*56}{X}\n")

    # ── Daily status ──────────────────────────────────────────────────────────

    def _print_daily_status(self, day: int):
        """Print end-of-day summary."""
        status = self._mes.get_status()
        w1     = status["warehouse_W1"]
        w2     = status["warehouse_W2"]

        with self._lock:
            in_transit = len(self._pending_deliveries)
            overdue    = sum(1 for r in self._penalty_records
                             if not r.delivered and day > r.ddate_day)

        print(f"\n{Fore.CYAN}[ERP] Day {day} Summary:{Style.RESET_ALL}")
        print(f"  W1         : Wood={w1['wood']} Metal={w1['metal']}")
        print(f"  W2         : {w2['wood'] + w2['metal']} pieces")
        print(f"  Orders     : Pending={status['pending']} "
              f"InProgress={status['in_progress']} "
              f"Completed={status['completed']}")
        print(f"  In transit : {in_transit} deliveries")
        print(f"  Overdue    : {overdue} orders")
        print(f"  Total cost : €{self._total_material_cost:.2f}")
        print(f"  Penalties  : €{self._total_penalties:.2f}")

    def _print_summary(self):
        """Print final summary on shutdown."""
        print(f"\n{Fore.CYAN}[ERP] Final Summary:{Style.RESET_ALL}")
        print(f"  Days elapsed      : {self._current_day}")
        print(f"  Material cost     : €{self._total_material_cost:.2f}")
        print(f"  Total penalties   : €{self._total_penalties:.2f}")
        print(f"  Net cost          : "
              f"€{self._total_material_cost + self._total_penalties:.2f}")

    # ── Public interface ──────────────────────────────────────────────────────

    def current_day(self) -> int:
        """Return current simulated day number."""
        return self._current_day

    def seconds_until_next_day(self) -> float:
        """Return seconds remaining in current day."""
        elapsed = time.time() - self._day_start
        return max(0.0, DAY_DURATION_S - elapsed)

    def get_pending_deliveries(self) -> list:
        """Return list of pending supplier deliveries."""
        with self._lock:
            return list(self._pending_deliveries)

    def get_financial_summary(self) -> dict:
        """Return cost and penalty summary."""
        return {
            "material_cost": self._total_material_cost,
            "penalties":     self._total_penalties,
            "net_cost":      self._total_material_cost + self._total_penalties,
        }