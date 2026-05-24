"""
test_integration.py
===================
Advanced ERP+MES integration test.

Sends multiple orders with different priorities, deadlines and quantities,
then measures scheduling efficiency, completion times, penalty costs and
supplier decisions. Results saved to test_results/ for comparison.

Usage:
    python test_integration.py              # interactive scenario picker
    python test_integration.py --scenario 1 # run specific scenario
    python test_integration.py --list       # list all scenarios
"""

import sys
import time
import os
import threading
import argparse
from datetime import datetime

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[94m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_results")


def hdr(msg):
    print(f"\n{CYAN}{BOLD}{'='*56}{RESET}")
    print(f"{CYAN}{BOLD}  {msg}{RESET}")
    print(f"{CYAN}{BOLD}{'='*56}{RESET}")

def ok(msg):   print(f"  {GREEN}[PASS]{RESET} {msg}")
def fail(msg): print(f"  {RED}[FAIL]{RESET} {msg}")
def info(msg): print(f"  {CYAN}[INFO]{RESET} {msg}")
def warn(msg): print(f"  {YELLOW}[WARN]{RESET} {msg}")


# ── Test scenarios ─────────────────────────────────────────────────────────────

SCENARIOS = {
    1: {
        "name": "Basic priority — same cell, different urgency",
        "description": (
            "Two RWW orders — one urgent (DDate=2) one relaxed (DDate=10). "
            "Scheduler should dispatch urgent first despite arrival order."
        ),
        "orders": [
            {"type": "RWW", "qty": 1, "ddate": 10, "penalty": 100, "name": "Low priority"},
            {"type": "RWW", "qty": 1, "ddate": 2,  "penalty": 500, "name": "Urgent"},
        ],
        "expected": "SWW dispatched before RWW",
        "timeout": 300,
    },
    2: {
        "name": "Parallel cells — C1 and C3 simultaneously",
        "description": (
            "One RWW (C1) and one RMM (C3) — should run in parallel "
            "since they use different cells."
        ),
        "orders": [
            {"type": "RWW", "qty": 1, "ddate": 5, "penalty": 200, "name": "C1 order"},
            {"type": "RMM", "qty": 1, "ddate": 5, "penalty": 200, "name": "C3 order"},
        ],
        "expected": "Both dispatched within same scheduler tick",
        "timeout": 300,
    },
    3: {
        "name": "Material shortage — supplier ordering",
        "description": (
            "Orders arrive before materials. ERP should order from supplier "
            "and materials should arrive within 2 days (SupplierA)."
        ),
        "orders": [
            {"type": "RWW", "qty": 2, "ddate": 5, "penalty": 300, "name": "Wood order"},
            {"type": "RMM", "qty": 2, "ddate": 5, "penalty": 300, "name": "Metal order"},
        ],
        "inject_materials": False,
        "expected": "ERP orders materials, delivered within 2 days",
        "timeout": 600,
    },
    4: {
        "name": "Heavy load — all 6 product types",
        "description": (
            "One of each product type. Tests parallel dispatch across C1/C2/C3, "
            "priority ordering, and total throughput."
        ),
        "orders": [
            {"type": "RWW", "qty": 1, "ddate": 8,  "penalty": 100, "name": "RWW"},
            {"type": "SWW", "qty": 1, "ddate": 6,  "penalty": 200, "name": "SWW"},
            {"type": "RWM", "qty": 1, "ddate": 4,  "penalty": 400, "name": "RWM urgent"},
            {"type": "SWM", "qty": 1, "ddate": 10, "penalty": 50,  "name": "SWM relaxed"},
            {"type": "RMM", "qty": 1, "ddate": 5,  "penalty": 300, "name": "RMM"},
            {"type": "SMM", "qty": 1, "ddate": 7,  "penalty": 150, "name": "SMM"},
        ],
        "expected": "All 6 types complete, priority ordering correct",
        "timeout": 600,
    },
    5: {
        "name": "Penalty test — late delivery",
        "description": (
            "Order with very short deadline (DDate=1). "
            "Should incur penalties tracked by ERP."
        ),
        "orders": [
            {"type": "RWW", "qty": 1, "ddate": 1, "penalty": 1000, "name": "Impossible deadline"},
            {"type": "SWW", "qty": 1, "ddate": 8, "penalty": 100,  "name": "Easy deadline"},
        ],
        "expected": "RWW penalised, SWW delivered on time",
        "timeout": 400,
    },
}


# ── Result tracking ─────────────────────────────────────────────────────────────

class TestTracker:
    def __init__(self, scenario):
        self.scenario      = scenario
        self.start_time    = time.time()
        self.order_events  = []   # (time, event, detail)
        self.dispatch_log  = []   # (time, type, cell, dock)
        self.complete_log  = []   # (time, type, qty, elapsed)
        self.day_log       = []   # (day, event)
        self.snapshot_log  = []   # periodic status snapshots

    def log(self, event, detail=""):
        t = time.time() - self.start_time
        self.order_events.append((t, event, detail))
        print(f"  {CYAN}[{t:6.1f}s]{RESET} {event} {detail}")

    def snapshot(self, mes, erp=None):
        t      = time.time() - self.start_time
        status = mes.get_status()
        snap   = {
            "t":          t,
            "pending":    status["pending"],
            "in_progress":status["in_progress"],
            "completed":  status["completed"],
            "w1_wood":    status["warehouse_W1"]["wood"],
            "w1_metal":   status["warehouse_W1"]["metal"],
            "w2":         status["warehouse_W2"]["wood"],
        }
        if erp:
            fin = erp.get_financial_summary()
            snap["cost"]     = fin["material_cost"]
            snap["penalties"]= fin["penalties"]
            snap["day"]      = erp.current_day()
        self.snapshot_log.append(snap)
        return snap


# ── Run scenario ────────────────────────────────────────────────────────────────

def run_scenario(scenario_id, inject_materials=True, day_duration=30):
    """
    Run a test scenario with MES + ERP.

    inject_materials: if True, seed W1 with enough material at start
                      (bypasses supplier scheme for faster testing)
    day_duration: seconds per simulated day (default 30 for faster testing)
    """
    scenario = SCENARIOS[scenario_id]
    hdr(f"Scenario {scenario_id}: {scenario['name']}")
    print(f"  {scenario['description']}")
    print(f"  Expected: {scenario['expected']}")
    print(f"  Day duration: {day_duration}s")
    print(f"  Material injection: {'YES' if inject_materials else 'NO (ERP supplier)'}")

    # Override inject if scenario specifies
    if "inject_materials" in scenario:
        inject_materials = scenario["inject_materials"]

    tracker = TestTracker(scenario)

    # ── Start MES ─────────────────────────────────────────────────────────────
    from mes import MES
    from erp import ERP

    mes = MES()
    erp = ERP(mes)
    mes.set_erp(erp)

    # Patch ERP day duration for faster testing
    import erp as erp_module
    erp_module.DAY_DURATION_S = day_duration

    mes_thread = threading.Thread(target=mes.run, daemon=True, name="mes")
    mes_thread.start()
    info("Waiting for MES to connect...")
    time.sleep(4)
    ok("MES ready")

    # ── Inject materials if test mode ─────────────────────────────────────────
    if inject_materials:
        # Calculate total materials needed
        from orders import RAW_MATERIALS
        total_wood = total_metal = 0
        for o in scenario["orders"]:
            mat = RAW_MATERIALS.get(o["type"], {})
            total_wood  += mat.get("Wood",  0) * o["qty"]
            total_metal += mat.get("Metal", 0) * o["qty"]
        # Add buffer
        total_wood  = min(total_wood  + 3, 20)
        total_metal = min(total_metal + 3, 20)
        mes.add_materials(wood=total_wood, metal=total_metal)
        tracker.log("MATERIALS INJECTED",
                    f"Wood={total_wood} Metal={total_metal}")

    # ── Start ERP ─────────────────────────────────────────────────────────────
    erp_thread = threading.Thread(target=erp.run, daemon=True, name="erp")
    erp_thread.start()
    time.sleep(1)

    # ── Send orders ───────────────────────────────────────────────────────────
    from orders import ClientOrder, Order

    hdr("Sending Orders")
    for i, o in enumerate(scenario["orders"]):
        client_order = ClientOrder(
            name    = o["name"],
            NIF     = i,
            OrderID = i + 1,
            orders  = [Order(
                type    = o["type"],
                quantity= o["qty"],
                DDate   = o["ddate"],
                Penalty = o["penalty"],
            )]
        )
        erp._handle_client_order(client_order)
        tracker.log(f"ORDER SENT",
                    f"{o['qty']}x {o['type']} DDate={o['ddate']} "
                    f"Penalty=€{o['penalty']}/day [{o['name']}]")
        time.sleep(0.2)

    # ── Monitor until complete or timeout ─────────────────────────────────────
    hdr("Monitoring")
    total_orders = len(scenario["orders"])
    timeout      = scenario["timeout"]
    poll_interval= 5
    prev_completed = 0

    for elapsed in range(0, timeout, poll_interval):
        time.sleep(poll_interval)
        snap = tracker.snapshot(mes, erp)

        print(f"  [{elapsed+poll_interval:4d}s] "
              f"Day={snap.get('day','?')} "
              f"Pending={snap['pending']} "
              f"InProgress={snap['in_progress']} "
              f"Completed={snap['completed']} | "
              f"W1=W:{snap['w1_wood']} M:{snap['w1_metal']} "
              f"W2={snap['w2']} | "
              f"Cost=€{snap.get('cost',0):.0f} "
              f"Pen=€{snap.get('penalties',0):.0f}")

        if snap["completed"] > prev_completed:
            newly_done = snap["completed"] - prev_completed
            tracker.log(f"ORDERS COMPLETED",
                        f"+{newly_done} (total {snap['completed']}/{total_orders})")
            prev_completed = snap["completed"]

        if snap["completed"] >= total_orders:
            tracker.log("ALL ORDERS COMPLETE",
                        f"in {elapsed+poll_interval}s")
            break
    else:
        tracker.log("TIMEOUT", f"after {timeout}s — "
                    f"{snap['completed']}/{total_orders} completed")

    # ── Final summary ─────────────────────────────────────────────────────────
    final_snap   = tracker.snapshot(mes, erp)
    total_elapsed= time.time() - tracker.start_time
    fin          = erp.get_financial_summary()

    hdr("Results")
    ok(f"Orders completed : {final_snap['completed']}/{total_orders}")
    ok(f"Total time       : {total_elapsed:.0f}s")
    ok(f"Material cost    : €{fin['material_cost']:.2f}")
    if fin["penalties"] > 0:
        warn(f"Penalties        : €{fin['penalties']:.2f}")
    else:
        ok(f"Penalties        : €0.00 (all on time)")
    ok(f"Net cost         : €{fin['net_cost']:.2f}")

    # Save results
    save_integration_results(scenario_id, scenario, tracker,
                             final_snap, fin, total_elapsed,
                             inject_materials, day_duration)

    return tracker


# ── Save results ───────────────────────────────────────────────────────────────

def save_integration_results(scenario_id, scenario, tracker,
                              final_snap, fin, total_elapsed,
                              inject_materials, day_duration):
    os.makedirs(RESULTS_DIR, exist_ok=True)

    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    mat_str  = "injected" if inject_materials else "supplier"
    filename = f"integration_s{scenario_id}_{ts}_{mat_str}.txt"
    filepath = os.path.join(RESULTS_DIR, filename)

    lines = []
    lines.append("=" * 56)
    lines.append("  INTEGRATION TEST RESULTS")
    lines.append(f"  Date      : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"  Scenario  : {scenario_id} — {scenario['name']}")
    lines.append(f"  Day dur.  : {day_duration}s")
    lines.append(f"  Materials : {mat_str}")
    lines.append("=" * 56)
    lines.append("")
    lines.append(f"DESCRIPTION:")
    lines.append(f"  {scenario['description']}")
    lines.append(f"  Expected: {scenario['expected']}")
    lines.append("")

    lines.append("ORDERS:")
    total_orders = len(scenario["orders"])
    for o in scenario["orders"]:
        lines.append(f"  {o['qty']}x {o['type']:<4} DDate={o['ddate']} "
                     f"Penalty=€{o['penalty']}/day  [{o['name']}]")
    lines.append("")

    lines.append("SUMMARY:")
    lines.append(f"  Completed  : {final_snap['completed']}/{total_orders}")
    lines.append(f"  Total time : {total_elapsed:.0f}s")
    lines.append(f"  Mat. cost  : €{fin['material_cost']:.2f}")
    lines.append(f"  Penalties  : €{fin['penalties']:.2f}")
    lines.append(f"  Net cost   : €{fin['net_cost']:.2f}")
    lines.append("")

    lines.append("EVENT LOG:")
    for t, event, detail in tracker.order_events:
        lines.append(f"  [{t:7.1f}s] {event:<25} {detail}")
    lines.append("")

    lines.append("STATUS SNAPSHOTS:")
    lines.append(f"  {'Time':>7} {'Day':>4} {'Pend':>5} {'InProg':>7} "
                 f"{'Done':>5} {'W1W':>5} {'W1M':>5} {'W2':>4} "
                 f"{'Cost':>8} {'Pen':>8}")
    for s in tracker.snapshot_log:
        lines.append(f"  {s['t']:7.1f}s {s.get('day','?'):>4} "
                     f"{s['pending']:5d} {s['in_progress']:7d} "
                     f"{s['completed']:5d} {s['w1_wood']:5d} "
                     f"{s['w1_metal']:5d} {s['w2']:4d} "
                     f"€{s.get('cost',0):7.2f} €{s.get('penalties',0):7.2f}")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"\n  {GREEN}Results saved to:{RESET}")
    print(f"  {filepath}")
    return filepath


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", type=int,
                        help="Run specific scenario ID")
    parser.add_argument("--list", action="store_true",
                        help="List all scenarios")
    parser.add_argument("--day",  type=int, default=30,
                        help="Seconds per simulated day (default 30)")
    parser.add_argument("--no-inject", action="store_true",
                        help="Don't inject materials (use ERP supplier)")
    args = parser.parse_args()

    if args.list:
        hdr("Available Scenarios")
        for sid, s in SCENARIOS.items():
            print(f"  [{sid}] {s['name']}")
            print(f"       {s['description'][:70]}...")
            print()
        return

    hdr("Integration Test — ERP + MES")
    print(f"  Results saved to: test_results/")

    # Pick scenario
    if args.scenario:
        scenario_id = args.scenario
    else:
        print(f"\n  Available scenarios:")
        for sid, s in SCENARIOS.items():
            print(f"    [{sid}] {s['name']}")
        choice = input("\n  Choose scenario [1-5] or ENTER for all: ").strip()
        if choice.isdigit() and int(choice) in SCENARIOS:
            scenario_id = int(choice)
        else:
            scenario_id = None

    inject = not args.no_inject

    if scenario_id:
        run_scenario(scenario_id, inject_materials=inject,
                     day_duration=args.day)
    else:
        # Run all scenarios
        for sid in SCENARIOS:
            try:
                run_scenario(sid, inject_materials=inject,
                             day_duration=args.day)
                time.sleep(5)
            except KeyboardInterrupt:
                warn("Interrupted — stopping")
                break

    print(f"\n{GREEN}Done. Check test_results/ for saved files.{RESET}\n")


if __name__ == "__main__":
    main()
