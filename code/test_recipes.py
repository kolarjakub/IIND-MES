"""
test_recipes.py
===============
Interactive recipe tester — test each product type across all capable cells.

Controls:
  ENTER       → test current recipe on next available cell
  q           → skip this product type entirely
  RWW/SWM/... → jump to specific product type
  x           → exit and show summary

For each product it shows the recipe slots, waits for confirmation,
dispatches to PLC, and polls until complete or timeout.

CODESYS and SFS must be running.
"""

import sys
import time
import threading
import os
from datetime import datetime

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[94m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

# Cell assignments confirmed from Final_Test PLC XML:
#   C1: T1,T2,T3,T8,T9,T11 → RWW (WoodLeg + WoodRoundTop)
#   C2: T1,T2,T3,T8,T9,T10 → SWW (WoodLeg + WoodSquareTop)
#   C3: T4,T5,T6,T8,T9,T11 → RMM (MetalLeg + MetalRoundTop)
#   C4: T4,T5,T6,T8,T9,T10 → SMM (MetalLeg + MetalSquareTop)
# RWM and SWM require multi-cell machining — NOT yet implemented in PLC.
CELL_OPTIONS = {
    "RWW": [("C1", 100)],
    "SWW": [("C2", 200)],
    "RMM": [("C3", 300)],
    "SMM": [("C4", 400)],
}

PRODUCT_TYPES = ["RWW", "SWW", "RMM", "SMM"]

MATERIAL_NEEDED = {
    "RWW": {"Wood": 3, "Metal": 0},
    "SWW": {"Wood": 3, "Metal": 0},
    "RMM": {"Wood": 0, "Metal": 3},
    "SMM": {"Wood": 0, "Metal": 3},
}

# Current estimates — updated after timing tests
CURRENT_ESTIMATES = {
    "RWW": 60,
    "SWW": 50,
    "RMM": 105,
    "SMM": 95,
}

# Measured times collected during this session: {(piece_type, cell): [t1, t2, ...]}
measured_times = {}

# Global ID counter — increments between tests so PLC never sees duplicate IDs
_next_proc_id = 901


def hdr(msg):
    print(f"\n{CYAN}{BOLD}{'='*56}{RESET}")
    print(f"{CYAN}{BOLD}  {msg}{RESET}")
    print(f"{CYAN}{BOLD}{'='*56}{RESET}")

def ok(msg):   print(f"  {GREEN}[PASS]{RESET} {msg}")
def fail(msg): print(f"  {RED}[FAIL]{RESET} {msg}")
def info(msg): print(f"  {CYAN}[INFO]{RESET} {msg}")
def warn(msg): print(f"  {YELLOW}[WARN]{RESET} {msg}")


def show_recipe(piece_type, cell_name, slots):
    from opcua_handler import ELocation, ETool

    CELL_LABELS = {
        ELocation.L: "Load  ", ELocation.T: "Trans ", ELocation.U: "Unload",
        ELocation.C1: "Cell1 ", ELocation.C2: "Cell2 ",
        ELocation.C3: "Cell3 ", ELocation.C4: "Cell4 ",
    }
    TOOL_LABELS = {
        ETool.IDLE: "IDLE", ETool.T1: "T1", ETool.T2: "T2",
        ETool.T3: "T3",    ETool.T4: "T4", ETool.T5: "T5",
        ETool.T6: "T6",    ETool.T8: "T8", ETool.T9: "T9",
        ETool.T10: "T10",  ETool.T11: "T11",
    }
    PHASE = ["Load","Load","Load","Mach","Mach","Mach",
             "Tran","Tran","Tran","Asm1","Asm2","Asm3","Unld"]

    mat = MATERIAL_NEEDED[piece_type]
    print(f"\n  {BOLD}{piece_type} → {cell_name}{RESET}")
    print(f"  Materials: Wood={mat['Wood']} Metal={mat['Metal']}")
    print(f"\n  {'#':3} {'Phase':5} {'Cell':8} {'Tool':5} {'Time':5} {'Asm':4} {'Type':4}")
    print(f"  {'─'*3} {'─'*5} {'─'*8} {'─'*5} {'─'*5} {'─'*4} {'─'*4}")
    for i, s in enumerate(slots):
        cell = CELL_LABELS.get(s["Cell"], str(s["Cell"]))
        tool = TOOL_LABELS.get(s["Tool"], str(s["Tool"]))
        asm  = "YES" if s["For_Assembly"] else ""
        print(f"  [{i:2d}] {PHASE[i]:5} {cell:8} {tool:5} {s['Tool_Time_Sec']:3d}s  "
              f"{asm:4} {s['Piece_Type']}")


def poll_result(h, timeout=180, w2_before=0):
    """
    Poll until all procedures go to 0.
    Records time at the moment procedures clear — this is when the
    PLC finished processing the piece.
    """
    start  = time.time()
    prev_n = -1

    for _ in range(timeout):
        time.sleep(1)
        elapsed = int(time.time() - start)
        try:
            procs  = h.read_procedures()
            inv    = h.read_warehouse_inventory()
            errors = h.read_errors()
            n      = len(procs)

            if n != prev_n:
                line = (f"  [{elapsed:3d}s] Procedures={n:2d} "
                        f"W1={inv['W1']} W2={inv['W2']}")
                print(f"{YELLOW if n > 0 else GREEN}{line}{RESET}")
                prev_n = n

            for e in errors:
                fail(f"PLC Error: code={e['code']} slot={e['slot']} proc={e['procedure_id']}")

            # Done when procedures clear
            if n == 0 and elapsed > 2:
                return True, elapsed

        except Exception as e:
            warn(f"Poll error: {e}")

    # Timeout — dump PLC state for diagnosis
    warn(f"Timeout after {timeout}s — dumping PLC state:")
    try:
        procs  = h.read_procedures()
        inv    = h.read_warehouse_inventory()
        errors = h.read_errors()
        warn(f"  Active procedures : {len(procs)}")
        for p in procs:
            warn(f"    proc id={p['id']} status={p['status']}")
        warn(f"  W1={inv['W1']} (Wood={inv['W1_wood']} Metal={inv['W1_metal']}) W2={inv['W2']}")
        if errors:
            for e in errors:
                fail(f"  Error: code={e['code']} slot={e['slot']} proc={e['procedure_id']}")
        else:
            warn(f"  No PLC errors reported")
    except Exception as ex:
        warn(f"  Could not read PLC state: {ex}")
    return False, timeout


def test_one(h, piece_type, cell_name, cell_loc):
    from opcua_handler import build_recipe

    global _next_proc_id
    slots = build_recipe(
        piece_type          = piece_type,
        id_recipe           = _next_proc_id // 100,
        id_procedure_start  = _next_proc_id,
        id_piece_start      = _next_proc_id * 10,
        id_final_piece      = _next_proc_id * 10 + 99,
    )
    _next_proc_id += 50   # leave gap between tests

    # Override cell location in machining + assembly slots (3-11)
    for i in range(3, 12):
        if slots[i]["Cell"] not in (0, 20, 30, 40, 50, 60):
            slots[i]["Cell"] = cell_loc

    show_recipe(piece_type, cell_name, slots)

    inv = h.read_warehouse_inventory()
    mat = MATERIAL_NEEDED[piece_type]
    print(f"\n  W1 now: Wood={inv['W1_wood']} Metal={inv['W1_metal']}")

    if inv["W1_wood"] < mat["Wood"] or inv["W1_metal"] < mat["Metal"]:
        warn(f"Need Wood={mat['Wood']} Metal={mat['Metal']} — add to SFS loading docks!")

    ans = input(f"\n  Dispatch {piece_type} to {cell_name}? "
                f"[ENTER=yes / q=skip / x=exit] ").strip().lower()
    if ans in ("q", "x"):
        return ans

    inv_before = h.read_warehouse_inventory()
    w2_before  = inv_before["W2"]

    info(f"Dispatching {piece_type} to {cell_name}...")
    if not h.dispatch(slots):
        fail("PLC rejected recipe")
        return False

    ok("PLC acknowledged — timing starts when piece enters W2")

    try:
        passed, elapsed = poll_result(h, timeout=180, w2_before=w2_before)
    except KeyboardInterrupt:
        warn("Aborted")
        return "q"

    if passed:
        inv2 = h.read_warehouse_inventory()
        ok(f"COMPLETED in {elapsed}s")
        ok(f"W2 after: {inv2['W2']} total, {inv2.get('W2_finished', 0)} finished")

        # Record measured time
        key = (piece_type, cell_name)
        if key not in measured_times:
            measured_times[key] = []
        measured_times[key].append(elapsed)

        # Compare to current estimate
        est = CURRENT_ESTIMATES.get(piece_type, 0)
        diff = elapsed - est
        if abs(diff) > 5:
            warn(f"Time differs from estimate: measured={elapsed}s "
                 f"estimate={est}s diff={diff:+d}s")
        else:
            ok(f"Time matches estimate: {elapsed}s ≈ {est}s")

        return True
    else:
        fail(f"Timed out after 120s — check SFS for stuck pieces")
        return False


def auto_test_one(h, piece_type, cell_name, cell_loc, pause=5, mes=None):
    """
    Run a recipe test automatically without keyboard input.
    pause: seconds to wait between dispatch attempts (for SFS to settle).
    Returns True/False/None.
    """
    from opcua_handler import build_recipe

    hdr(f"AUTO: {piece_type} on {cell_name}")

    global _next_proc_id
    slots = build_recipe(
        piece_type          = piece_type,
        id_recipe           = _next_proc_id // 100,
        id_procedure_start  = _next_proc_id,
        id_piece_start      = _next_proc_id * 10,
        id_final_piece      = _next_proc_id * 10 + 99,
    )
    _next_proc_id += 50   # leave gap between tests

    # Override cell location in machining + assembly slots (3-11)
    for i in range(3, 12):
        if slots[i]["Cell"] not in (0, 20, 30, 40, 50, 60):
            slots[i]["Cell"] = cell_loc

    show_recipe(piece_type, cell_name, slots)

    # Check materials — inject via MES if available, else wait for SFS
    mat = MATERIAL_NEEDED[piece_type]

    inv = h.read_warehouse_inventory()
    print(f"\n  W1: Wood={inv['W1_wood']} Metal={inv['W1_metal']}")

    if inv["W1_wood"] < mat["Wood"] or inv["W1_metal"] < mat["Metal"]:
        if mes is not None:
            # Test mode — inject materials directly via MES
            need_wood  = max(0, mat["Wood"]  - inv["W1_wood"])
            need_metal = max(0, mat["Metal"] - inv["W1_metal"])
            info(f"Injecting +{need_wood} Wood +{need_metal} Metal via MES...")
            mes.add_materials(wood=need_wood, metal=need_metal)
            time.sleep(1)  # let tracking update
        else:
            # Standalone mode — wait for human to add to SFS
            warn(f"Need Wood={mat['Wood']} Metal={mat['Metal']} in W1")
            warn(f"Add pieces to SFS loading docks — waiting up to 120s...")
            wait_elapsed = 0
            while wait_elapsed < 120:
                time.sleep(2)
                wait_elapsed += 2
                inv = h.read_warehouse_inventory()
                print(f"\r  [{wait_elapsed:3d}s] W1: Wood={inv['W1_wood']} "
                      f"Metal={inv['W1_metal']} — waiting...",
                      end="", flush=True)
                if inv["W1_wood"] >= mat["Wood"] and inv["W1_metal"] >= mat["Metal"]:
                    print()
                    ok(f"Material ready after {wait_elapsed}s")
                    break
            else:
                print()
                fail(f"No material after 120s — skipping {piece_type}")
                return False

    if pause > 0 and mes is None:
        info(f"Starting in {pause}s...")
        time.sleep(pause)

    inv_before = h.read_warehouse_inventory()
    w2_before  = inv_before["W2"]

    info(f"Dispatching {piece_type} to {cell_name}...")
    if not h.dispatch(slots):
        fail("PLC rejected recipe")
        return False

    ok("PLC acknowledged — timing starts when piece enters W2")

    try:
        passed, elapsed = poll_result(h, timeout=180, w2_before=w2_before)
    except KeyboardInterrupt:
        warn("Aborted by user")
        return None

    if passed:
        inv2 = h.read_warehouse_inventory()
        ok(f"COMPLETED in {elapsed}s")
        ok(f"W2: {inv2['W2']} total, {inv2.get('W2_finished', 0)} finished")

        key = (piece_type, cell_name)
        if key not in measured_times:
            measured_times[key] = []
        measured_times[key].append(elapsed)

        est  = CURRENT_ESTIMATES.get(piece_type, 0)
        diff = elapsed - est
        if abs(diff) > 5:
            warn(f"measured={elapsed}s estimate={est}s diff={diff:+d}s")
        else:
            ok(f"Time matches estimate: {elapsed}s ≈ {est}s")

        return True
    else:
        fail("Timed out after 180s")
        return False


RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_results")


def save_results(results, measured_times, auto, use_mes, repeat):
    """Save test results to a timestamped txt file in test_results/."""
    os.makedirs(RESULTS_DIR, exist_ok=True)

    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    mode_str = "auto" if auto else "interactive"
    mat_str  = "mes_inject" if use_mes else "manual"
    filename = f"recipe_test_{ts}_{mode_str}_{mat_str}.txt"
    filepath = os.path.join(RESULTS_DIR, filename)

    lines = []
    lines.append("=" * 56)
    lines.append("  RECIPE TEST RESULTS")
    lines.append(f"  Date    : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"  Mode    : {mode_str.upper()}")
    lines.append(f"  Material: {mat_str}")
    lines.append(f"  Repeats : {repeat}")
    lines.append("=" * 56)
    lines.append("")

    # Pass/Fail table
    lines.append("RESULTS:")
    lines.append(f"  {'Product':<8} {'Cell':<4} {'Result'}")
    lines.append(f"  {'─'*8} {'─'*4} {'─'*10}")
    for ptype in PRODUCT_TYPES:
        for cell_name, _ in CELL_OPTIONS[ptype]:
            key = f"{ptype}@{cell_name}"
            r = results.get(key, "NOT RUN")
            lines.append(f"  {ptype:<8} {cell_name:<4} {r}")

    total   = len([r for r in results.values() if r != "NOT RUN"])
    passed  = len([r for r in results.values() if r == "PASS"])
    failed  = len([r for r in results.values() if r == "FAIL"])
    skipped = len([r for r in results.values() if r == "SKIPPED"])
    lines.append("")
    lines.append(f"  Total={total}  PASS={passed}  FAIL={failed}  SKIPPED={skipped}")

    # Timing table
    if measured_times:
        lines.append("")
        lines.append("TIMING:")
        lines.append(f"  {'Product':<8} {'Cell':<4} {'Times':<25} {'Avg':>6} {'Estimate':>9} {'Diff':>6}")
        lines.append(f"  {'─'*8} {'─'*4} {'─'*25} {'─'*6} {'─'*9} {'─'*6}")
        for (ptype, cell_name), times in sorted(measured_times.items()):
            avg  = sum(times) / len(times)
            est  = CURRENT_ESTIMATES.get(ptype, 0)
            diff = avg - est
            times_str = " ".join(f"{t}s" for t in times)
            lines.append(f"  {ptype:<8} {cell_name:<4} {times_str:<25} "
                         f"{avg:5.0f}s {est:8d}s {diff:+6.0f}s")

        lines.append("")
        lines.append("SUGGESTED ESTIMATED_TIME:")
        lines.append("  ESTIMATED_TIME = {")
        updated = {}
        for (ptype, _), times in measured_times.items():
            if ptype not in updated:
                updated[ptype] = round(sum(times) / len(times))
        for ptype in PRODUCT_TYPES:
            if ptype in updated:
                old_val = CURRENT_ESTIMATES[ptype]
                new_val = updated[ptype]
                marker = f"  # was {old_val}s" if new_val != old_val else ""
                lines.append(f'      "{ptype}": {new_val},{marker}')
            else:
                lines.append(f'      "{ptype}": {CURRENT_ESTIMATES[ptype]},  # not tested')
        lines.append("  }")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"\n  {GREEN}Results saved to:{RESET}")
    print(f"  {filepath}")
    return filepath


def main():
    print(f"  Tests every product type on every capable cell.")
    print(f"  Timing stops when piece physically enters W2.\n")

    print(f"  Mode:")
    print(f"    [1] Interactive — confirm each test manually")
    print(f"    [2] Auto        — run all tests automatically")
    mode_choice = input("  Choose [1/2]: ").strip()
    auto = (mode_choice == "2")

    pause  = 5
    only   = None
    repeat = 1

    if auto:
        print(f"\n  Pause between tests in seconds [default 5]: ", end="")
        p = input().strip()
        pause = int(p) if p.isdigit() else 5

        print(f"  Repeat each test N times for better average [default 1]: ", end="")
        r = input().strip()
        repeat = int(r) if r.isdigit() else 1

        print(f"  Only test specific types? (e.g. RWW RMM) or ENTER for all: ", end="")
        o = input().strip().upper()
        only = o.split() if o else None

    mode = "AUTO" if auto else "INTERACTIVE"
    print(f"\n  {BOLD}Mode: {mode}{RESET}")
    if auto:
        print(f"  Pause={pause}s  Repeats={repeat}  Only={only or 'all'}")

    # Ask about material injection mode
    print(f"\n  Material source:")
    print(f"    [1] Manual   — add pieces to SFS loading docks yourself")
    print(f"    [2] MES test — inject materials automatically via MES (no ERP)")
    mat_choice = input("  Choose [1/2]: ").strip()
    use_mes = (mat_choice == "2")

    from opcua_handler import OpcUaHandler
    h = OpcUaHandler()
    try:
        h.connect()
        ok("Connected to CODESYS")
    except Exception as e:
        fail(f"Connection failed: {e}")
        sys.exit(1)

    inv = h.read_warehouse_inventory()
    info(f"W1: Wood={inv['W1_wood']} Metal={inv['W1_metal']} "
         f"| W2: {inv['W2']} pieces")

    # Start MES in test mode if requested
    mes = None
    if use_mes:
        from mes import MES
        mes = MES()
        mes_thread = threading.Thread(target=mes.run, daemon=True, name="mes")
        mes_thread.start()
        info("MES starting in test mode — waiting 4s...")
        time.sleep(4)
        ok("MES ready — will inject materials automatically")
    elif auto:
        warn("Add pieces to SFS loading docks before each test!")
        input("  Press ENTER when ready... ")

    # Build full test list: (product, cell_name, cell_loc)
    tests = []
    for ptype in PRODUCT_TYPES:
        if only and ptype not in [x.upper() for x in only]:
            continue
        for cell_name, cell_loc in CELL_OPTIONS[ptype]:
            for _ in range(repeat):
                tests.append((ptype, cell_name, cell_loc))

    results = {}

    if auto:
        # ── Automatic mode ────────────────────────────────────────────────────
        info(f"Running {len(tests)} test(s) automatically...")
        for i, (ptype, cell_name, cell_loc) in enumerate(tests):
            key = f"{ptype}@{cell_name}"
            print(f"\n{BOLD}  [{i+1}/{len(tests)}] {ptype} on {cell_name}{RESET}")
            result = auto_test_one(h, ptype, cell_name, cell_loc, pause=pause, mes=mes)
            if result is True:
                results[key] = "PASS"
            elif result is False:
                results[key] = "FAIL"
            else:
                results[key] = "ABORTED"
                break
            if i < len(tests) - 1:
                info(f"Waiting {pause}s before next test...")
                time.sleep(pause)

    else:
        # ── Interactive mode ──────────────────────────────────────────────────
        idx = 0
        while idx < len(tests):
            ptype, cell_name, cell_loc = tests[idx]
            key = f"{ptype}@{cell_name}"

            print(f"\n{BOLD}  [{idx+1}/{len(tests)}] {ptype} on {cell_name}{RESET}")
            print(f"  [ENTER] test  [q] skip  [x] exit  "
                  f"[type e.g. RWM] jump to product")
            cmd = input("  > ").strip()

            if cmd.upper() in PRODUCT_TYPES:
                target = cmd.upper()
                new_idx = next((i for i, t in enumerate(tests)
                                if t[0] == target), None)
                if new_idx is not None:
                    idx = new_idx
                    continue
                else:
                    warn(f"{target} not in test list")
                    continue

            if cmd.lower() == "x":
                break
            if cmd.lower() == "q":
                results[key] = "SKIPPED"
                idx += 1
                continue

            result = test_one(h, ptype, cell_name, cell_loc)

            if result == "x":
                break
            elif result == "q":
                results[key] = "SKIPPED"
            elif result is True:
                results[key] = "PASS"
            else:
                results[key] = "FAIL"

            idx += 1

    # Summary
    hdr("Results")
    for ptype in PRODUCT_TYPES:
        cells = CELL_OPTIONS[ptype]
        for cell_name, _ in cells:
            key = f"{ptype}@{cell_name}"
            r = results.get(key, "NOT RUN")
            color = (GREEN if r == "PASS" else
                     RED   if r == "FAIL" else
                     YELLOW)
            print(f"  {ptype} → {cell_name:2s} : {color}{r}{RESET}")

    # Timing report
    if measured_times:
        hdr("Timing Report")
        print(f"  {'Product':6} {'Cell':4} {'Times':20} {'Avg':6} {'Current est':12} {'Diff':6}")
        print(f"  {'─'*6} {'─'*4} {'─'*20} {'─'*6} {'─'*12} {'─'*6}")
        for (ptype, cell_name), times in sorted(measured_times.items()):
            avg = sum(times) / len(times)
            est = CURRENT_ESTIMATES.get(ptype, 0)
            diff = avg - est
            times_str = " ".join(f"{t}s" for t in times)
            color = RED if abs(diff) > 10 else YELLOW if abs(diff) > 5 else GREEN
            print(f"  {ptype:6} {cell_name:4} {times_str:20} "
                  f"{avg:5.0f}s {est:5d}s est   "
                  f"{color}{diff:+.0f}s{RESET}")

        print(f"\n  {BOLD}Suggested ESTIMATED_TIME updates:{RESET}")
        updated = {}
        for (ptype, cell_name), times in measured_times.items():
            avg = round(sum(times) / len(times))
            if ptype not in updated:
                updated[ptype] = avg
        print(f"  ESTIMATED_TIME = {{")
        for ptype in PRODUCT_TYPES:
            if ptype in updated:
                old_val = CURRENT_ESTIMATES[ptype]
                new_val = updated[ptype]
                marker = f"  # was {old_val}s" if new_val != old_val else ""
                print(f'    "{ptype}": {new_val},{marker}')
            else:
                print(f'    "{ptype}": {CURRENT_ESTIMATES[ptype]},  # not tested')
        print("  }}")

    save_results(results, measured_times, auto, use_mes, repeat)

    h.disconnect()
    print()


if __name__ == "__main__":
    main()