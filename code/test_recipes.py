"""
test_recipes.py
===============
Interactive / automatic recipe tester -- exercises every product type.

Controls (interactive mode):
  ENTER         -> test next recipe
  q             -> skip this product
  RWW / SWM ... -> jump directly to that product type
  x             -> exit and show summary

For each recipe the script shows the slot table, checks material availability,
dispatches to the PLC, and polls until the piece completes or times out.
Measured times are collected and compared against current estimates at the end.

Requires CODESYS + SFS to be running.

Product types:
  RWW  Wood Round Table          (all wood,  C1, T8)
  SWW  Wood Square Table         (all wood,  C2, T8)
  RWM  Wood Round Top+Metal Legs (mixed,     C3, T9) -- multi-cell machining
  SWM  Wood Square Top+Metal Legs(mixed,     C4, T9) -- multi-cell machining
  RMM  Metal Round Table         (all metal, C3, T8)
  SMM  Metal Square Table        (all metal, C4, T8)
"""

import sys
import time
import os
from datetime import datetime

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[94m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

# Assembly cell for display / result labelling (machining may use other cells).
# mach_note is shown in the slot table header for multi-cell recipes.
CELL_OPTIONS = {
    "RWW": [("C1", 100)],
    "SWW": [("C2", 200)],
    "RWM": [("C3", 300)],   # legs machined at C3, top at C1
    "SWM": [("C4", 400)],   # legs machined at C4, top at C2
    "RMM": [("C3", 300)],
    "SMM": [("C4", 400)],
}

PRODUCT_TYPES = ["RWW", "SWW", "RWM", "SWM", "RMM", "SMM"]

MATERIAL_NEEDED = {
    "RWW": {"Wood": 3, "Metal": 0},
    "SWW": {"Wood": 3, "Metal": 0},
    "RWM": {"Wood": 1, "Metal": 2},
    "SWM": {"Wood": 1, "Metal": 2},
    "RMM": {"Wood": 0, "Metal": 3},
    "SMM": {"Wood": 0, "Metal": 3},
}

# Current time estimates in seconds -- updated from measured data after runs.
CURRENT_ESTIMATES = {
    "RWW": 60,
    "SWW": 50,
    "RWM": 100,
    "SWM": 90,
    "RMM": 105,
    "SMM": 95,
}

# Measured times this session: {(piece_type, cell_name): [t1, t2, ...]}
measured_times: dict = {}

# Global procedure-ID counter -- keeps IDs unique across all tests.
_next_proc_id = 901


# -- Helpers ------------------------------------------------------------------

def hdr(msg):
    print(f"\n{CYAN}{BOLD}{'='*58}{RESET}")
    print(f"{CYAN}{BOLD}  {msg}{RESET}")
    print(f"{CYAN}{BOLD}{'='*58}{RESET}")

def ok(msg):   print(f"  {GREEN}[PASS]{RESET} {msg}")
def fail(msg): print(f"  {RED}[FAIL]{RESET} {msg}")
def info(msg): print(f"  {CYAN}[INFO]{RESET} {msg}")
def warn(msg): print(f"  {YELLOW}[WARN]{RESET} {msg}")


def show_recipe(piece_type: str, cell_name: str, slots: list):
    """Pretty-print the 13-slot recipe table."""
    from opcua_handler import ELocation, ETool, PRODUCT_RECIPES

    CELL_LABELS = {
        ELocation.L:  "Load  ", ELocation.T:  "Trans ",
        ELocation.U:  "Unload", ELocation.C1: "Cell1 ",
        ELocation.C2: "Cell2 ", ELocation.C3: "Cell3 ",
        ELocation.C4: "Cell4 ",
    }
    TOOL_LABELS = {
        ETool.IDLE: "IDLE",
        ETool.T1: "T1", ETool.T2: "T2", ETool.T3: "T3",
        ETool.T4: "T4", ETool.T5: "T5", ETool.T6: "T6",
        ETool.T8: "T8", ETool.T9: "T9",
        ETool.T10: "T10", ETool.T11: "T11",
    }
    PHASE = ["Load","Load","Load","Mach","Mach","Mach",
             "Tran","Tran","Tran","Asm1","Asm2","Asm3","Unld"]

    r   = PRODUCT_RECIPES[piece_type]
    mat = MATERIAL_NEEDED[piece_type]

    is_multi = r["mach_cells"] is not None
    note     = f"  {YELLOW}[multi-cell machining]{RESET}" if is_multi else ""

    print(f"\n  {BOLD}{piece_type} -> asm {cell_name}{RESET}{note}")
    print(f"  Materials required: Wood={mat['Wood']}  Metal={mat['Metal']}")
    print(f"\n  {'#':3} {'Phase':5} {'Cell':8} {'Tool':5} {'Time':5} "
          f"{'Asm':4} {'Mat':4} {'Type'}")
    print(f"  {'-'*3} {'-'*5} {'-'*8} {'-'*5} {'-'*5} {'-'*4} {'-'*4} {'-'*4}")

    for i, s in enumerate(slots):
        cell_lbl = CELL_LABELS.get(s["Cell"], f"{s['Cell']:3d}")
        tool_lbl = TOOL_LABELS.get(s["Tool"], f"T{s['Tool']}")
        asm      = "YES" if s["For_Assembly"] else ""
        print(f"  [{i:2d}] {PHASE[i]:5} {cell_lbl:8} {tool_lbl:5} "
              f"{s['Tool_Time_Sec']:3d}s  {asm:4} "
              f"{s['Piece_Material']}    {s['Piece_Type']}")


def _build_test_slots(piece_type: str) -> list:
    """Build recipe slots with the global _next_proc_id counter."""
    from opcua_handler import build_recipe
    global _next_proc_id

    slots = build_recipe(
        piece_type         = piece_type,
        id_recipe          = _next_proc_id // 100,
        id_procedure_start = _next_proc_id,
        id_piece_start     = _next_proc_id * 10,
        id_final_piece     = _next_proc_id * 10 + 99,
    )
    _next_proc_id += 50    # leave a gap so IDs are never reused
    return slots


def poll_result(h, timeout: int = 180) -> tuple:
    """
    Poll until all active procedures clear (count reaches 0).
    Returns (passed: bool, elapsed_seconds: int).
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
                colour = YELLOW if n > 0 else GREEN
                print(f"{colour}  [{elapsed:3d}s] Procs={n:2d}  "
                      f"W1={inv['W1']}  W2={inv['W2']}{RESET}")
                prev_n = n

            for e in errors:
                fail(f"PLC Error: code={e['code']}  slot={e['slot']}  "
                     f"proc={e['procedure_id']}")

            if n == 0 and elapsed > 2:
                return True, elapsed

        except Exception as e:
            warn(f"Poll error: {e}")

    # Timeout -- dump PLC state for diagnosis
    warn(f"Timeout after {timeout}s -- PLC state:")
    try:
        procs  = h.read_procedures()
        inv    = h.read_warehouse_inventory()
        errors = h.read_errors()
        warn(f"  Active procedures : {len(procs)}")
        for p in procs:
            warn(f"    id={p['id']}  status={p['status']}")
        warn(f"  W1: total={inv['W1']}  "
             f"wood={inv['W1_wood']}  metal={inv['W1_metal']}")
        warn(f"  W2: total={inv['W2']}  finished={inv['W2_finished']}")
        if errors:
            for e in errors:
                fail(f"  Error: code={e['code']}  slot={e['slot']}  "
                     f"proc={e['procedure_id']}")
        else:
            warn("  No PLC errors reported")
    except Exception as ex:
        warn(f"  Could not read PLC state: {ex}")

    return False, timeout


def _check_and_wait_materials(h, piece_type: str, timeout: int = 120,
                               mes=None) -> bool:
    """
    Ensure W1 has enough material for piece_type.
    If `mes` is supplied, inject via MES.  Otherwise wait up to `timeout` s.
    Returns True when materials are available.
    """
    mat = MATERIAL_NEEDED[piece_type]
    inv = h.read_warehouse_inventory()
    print(f"\n  W1 now: Wood={inv['W1_wood']}  Metal={inv['W1_metal']}")

    need_wood  = max(0, mat["Wood"]  - inv["W1_wood"])
    need_metal = max(0, mat["Metal"] - inv["W1_metal"])

    if need_wood == 0 and need_metal == 0:
        return True

    if mes is not None:
        info(f"Injecting +{need_wood} Wood  +{need_metal} Metal via MES...")
        mes.add_materials(wood=need_wood, metal=need_metal)
        time.sleep(1)
        return True

    warn(f"Need  Wood={mat['Wood']}  Metal={mat['Metal']}  -- "
         f"add to SFS loading docks.  Waiting up to {timeout}s...")
    for elapsed in range(0, timeout, 2):
        time.sleep(2)
        inv = h.read_warehouse_inventory()
        print(f"\r  [{elapsed+2:3d}s] W1: Wood={inv['W1_wood']}  "
              f"Metal={inv['W1_metal']} -- waiting...",
              end="", flush=True)
        if inv["W1_wood"] >= mat["Wood"] and inv["W1_metal"] >= mat["Metal"]:
            print()
            ok(f"Material ready after {elapsed+2}s")
            return True
    print()
    fail(f"No material after {timeout}s -- skipping {piece_type}")
    return False


def _record_time(piece_type: str, cell_name: str, elapsed: int):
    key = (piece_type, cell_name)
    measured_times.setdefault(key, []).append(elapsed)
    est  = CURRENT_ESTIMATES.get(piece_type, 0)
    diff = elapsed - est
    if abs(diff) > 5:
        warn(f"measured={elapsed}s  estimate={est}s  diff={diff:+d}s")
    else:
        ok(f"Time matches estimate: {elapsed}s ~ {est}s")


# -- Interactive test ---------------------------------------------------------

def test_one(h, piece_type: str, cell_name: str) -> object:
    """
    Run one interactive test.
    Returns True=pass, False=fail, "q"=skip, "x"=exit.
    """
    slots = _build_test_slots(piece_type)
    show_recipe(piece_type, cell_name, slots)

    mat = MATERIAL_NEEDED[piece_type]
    inv = h.read_warehouse_inventory()
    if inv["W1_wood"] < mat["Wood"] or inv["W1_metal"] < mat["Metal"]:
        warn(f"Need Wood={mat['Wood']} Metal={mat['Metal']} -- "
             f"add to SFS loading docks!")

    ans = input(f"\n  Dispatch {piece_type} to {cell_name}? "
                f"[ENTER=yes  q=skip  x=exit] ").strip().lower()
    if ans in ("q", "x"):
        return ans

    if not _check_and_wait_materials(h, piece_type):
        return False

    info(f"Dispatching {piece_type}...")
    if not h.dispatch(slots):
        fail("PLC rejected recipe")
        return False

    ok("PLC acknowledged -- polling...")

    try:
        passed, elapsed = poll_result(h, timeout=180)
    except KeyboardInterrupt:
        warn("Aborted")
        return "q"

    if passed:
        inv2 = h.read_warehouse_inventory()
        ok(f"COMPLETED in {elapsed}s")
        ok(f"W2: {inv2['W2']} total  {inv2['W2_finished']} finished")
        _record_time(piece_type, cell_name, elapsed)
        return True
    else:
        fail(f"Timed out after 180s -- check SFS for stuck pieces")
        return False


# -- Automatic test -----------------------------------------------------------

def auto_test_one(h, piece_type: str, cell_name: str,
                  pause: int = 5, mes=None) -> object:
    """
    Run one automatic test.
    Returns True=pass, False=fail, None=aborted.
    """
    hdr(f"AUTO: {piece_type} on {cell_name}")

    slots = _build_test_slots(piece_type)
    show_recipe(piece_type, cell_name, slots)

    if not _check_and_wait_materials(h, piece_type, mes=mes):
        return False

    if pause > 0 and mes is None:
        info(f"Starting in {pause}s...")
        time.sleep(pause)

    info(f"Dispatching {piece_type}...")
    if not h.dispatch(slots):
        fail("PLC rejected recipe")
        return False

    ok("PLC acknowledged -- polling...")

    try:
        passed, elapsed = poll_result(h, timeout=180)
    except KeyboardInterrupt:
        warn("Aborted by user")
        return None

    if passed:
        inv2 = h.read_warehouse_inventory()
        ok(f"COMPLETED in {elapsed}s")
        ok(f"W2: {inv2['W2']} total  {inv2['W2_finished']} finished")
        _record_time(piece_type, cell_name, elapsed)
        return True
    else:
        fail("Timed out after 180s")
        return False


# -- Results save -------------------------------------------------------------

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "test_results")


def save_results(results: dict, auto: bool, use_mes: bool, repeat: int) -> str:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    mode_str = "auto" if auto else "interactive"
    mat_str  = "mes_inject" if use_mes else "manual"
    filepath = os.path.join(RESULTS_DIR,
                            f"recipe_test_{ts}_{mode_str}_{mat_str}.txt")

    lines = [
        "=" * 58,
        "  RECIPE TEST RESULTS",
        f"  Date    : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"  Mode    : {mode_str.upper()}",
        f"  Material: {mat_str}",
        f"  Repeats : {repeat}",
        "=" * 58,
        "",
        "RESULTS:",
        f"  {'Product':<8} {'Cell':<4} {'Result'}",
        f"  {'-'*8} {'-'*4} {'-'*10}",
    ]

    for ptype in PRODUCT_TYPES:
        for cell_name, _ in CELL_OPTIONS[ptype]:
            key = f"{ptype}@{cell_name}"
            lines.append(f"  {ptype:<8} {cell_name:<4} "
                         f"{results.get(key, 'NOT RUN')}")

    total   = len([r for r in results.values() if r != "NOT RUN"])
    passed  = sum(1 for r in results.values() if r == "PASS")
    failed  = sum(1 for r in results.values() if r == "FAIL")
    skipped = sum(1 for r in results.values() if r == "SKIPPED")
    lines += ["",
              f"  Total={total}  PASS={passed}  FAIL={failed}  "
              f"SKIPPED={skipped}"]

    if measured_times:
        lines += ["", "TIMING:",
                  f"  {'Product':<8} {'Cell':<4} {'Times':<25} "
                  f"{'Avg':>6} {'Estimate':>9} {'Diff':>7}",
                  f"  {'-'*8} {'-'*4} {'-'*25} {'-'*6} {'-'*9} {'-'*7}"]
        for (ptype, cell_name), times in sorted(measured_times.items()):
            avg  = sum(times) / len(times)
            est  = CURRENT_ESTIMATES.get(ptype, 0)
            diff = avg - est
            times_str = " ".join(f"{t}s" for t in times)
            lines.append(f"  {ptype:<8} {cell_name:<4} {times_str:<25} "
                         f"{avg:5.0f}s  {est:6d}s  {diff:+6.0f}s")

        lines += ["", "SUGGESTED ESTIMATED_TIME:",
                  "  ESTIMATED_TIME = {"]
        updated = {}
        for (ptype, _), times in measured_times.items():
            updated.setdefault(ptype, round(sum(times) / len(times)))
        for ptype in PRODUCT_TYPES:
            if ptype in updated:
                old  = CURRENT_ESTIMATES[ptype]
                new  = updated[ptype]
                note = f"  # was {old}s" if new != old else ""
                lines.append(f'    "{ptype}": {new},{note}')
            else:
                lines.append(f'    "{ptype}": '
                             f'{CURRENT_ESTIMATES[ptype]},  # not tested')
        lines.append("  }")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"\n  {GREEN}Results saved to:{RESET}")
    print(f"  {filepath}")
    return filepath


# -- Main ---------------------------------------------------------------------

def main():
    print(f"\n  Flexible Production Line -- Recipe Tester")
    print(f"  Tests all 6 product types on their capable cells.\n")

    print("  Mode:")
    print("    [1] Interactive -- confirm each test manually")
    print("    [2] Auto        -- run all tests automatically")
    mode_choice = input("  Choose [1/2]: ").strip()
    auto = (mode_choice == "2")

    pause  = 5
    only   = None
    repeat = 1

    if auto:
        print("\n  Pause between tests in seconds [default 5]: ", end="")
        p = input().strip()
        pause = int(p) if p.isdigit() else 5

        print("  Repeat each test N times for better averages [default 1]: ",
              end="")
        r = input().strip()
        repeat = int(r) if r.isdigit() else 1

        print("  Only test specific types? (e.g. RWW RWM) or ENTER for all: ",
              end="")
        o = input().strip().upper()
        only = o.split() if o else None

    mode = "AUTO" if auto else "INTERACTIVE"
    print(f"\n  {BOLD}Mode: {mode}{RESET}")
    if auto:
        print(f"  Pause={pause}s  Repeats={repeat}  "
              f"Only={only or 'all'}")

    print("\n  Material source:")
    print("    [1] Manual   -- add pieces to SFS loading docks yourself")
    print("    [2] MES test -- inject materials automatically via MES")
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
    info(f"W1: Wood={inv['W1_wood']}  Metal={inv['W1_metal']}  "
         f"| W2: {inv['W2']} pieces  ({inv['W2_finished']} finished)")

    mes = None
    if use_mes:
        from mes import MES
        mes = MES()
        import threading
        mes_thread = threading.Thread(target=mes.run, daemon=True, name="mes")
        mes_thread.start()
        info("MES starting in test mode -- waiting 4s...")
        time.sleep(4)
        ok("MES ready -- will inject materials automatically")
    elif auto:
        warn("Add pieces to SFS loading docks before each test!")
        input("  Press ENTER when ready... ")

    # Build test list: (product_type, cell_name)
    tests = []
    for ptype in PRODUCT_TYPES:
        if only and ptype not in [x.upper() for x in only]:
            continue
        for cell_name, _ in CELL_OPTIONS[ptype]:
            for _ in range(repeat):
                tests.append((ptype, cell_name))

    results = {}

    if auto:
        # -- Automatic mode ---------------------------------------------------
        info(f"Running {len(tests)} test(s) automatically...")
        for i, (ptype, cell_name) in enumerate(tests):
            key = f"{ptype}@{cell_name}"
            print(f"\n{BOLD}  [{i+1}/{len(tests)}] {ptype} on {cell_name}{RESET}")
            result = auto_test_one(h, ptype, cell_name, pause=pause, mes=mes)
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
        # -- Interactive mode -------------------------------------------------
        idx = 0
        while idx < len(tests):
            ptype, cell_name = tests[idx]
            key = f"{ptype}@{cell_name}"

            print(f"\n{BOLD}  [{idx+1}/{len(tests)}] {ptype} on {cell_name}{RESET}")
            print("  [ENTER] test  [q] skip  [x] exit  "
                  "[type e.g. RWM] jump to product")
            cmd = input("  > ").strip()

            if cmd.upper() in PRODUCT_TYPES:
                target  = cmd.upper()
                new_idx = next((i for i, t in enumerate(tests)
                                if t[0] == target), None)
                if new_idx is not None:
                    idx = new_idx
                else:
                    warn(f"{target} not in test list")
                continue

            if cmd.lower() == "x":
                break
            if cmd.lower() == "q":
                results[key] = "SKIPPED"
                idx += 1
                continue

            result = test_one(h, ptype, cell_name)

            if result == "x":
                break
            elif result == "q":
                results[key] = "SKIPPED"
            elif result is True:
                results[key] = "PASS"
            else:
                results[key] = "FAIL"
            idx += 1

    # -- Summary --------------------------------------------------------------
    hdr("Results")
    for ptype in PRODUCT_TYPES:
        for cell_name, _ in CELL_OPTIONS[ptype]:
            key = f"{ptype}@{cell_name}"
            r   = results.get(key, "NOT RUN")
            colour = (GREEN  if r == "PASS"    else
                      RED    if r == "FAIL"    else
                      YELLOW)
            multi  = " *" if ptype in ("RWM", "SWM") else ""
            print(f"  {ptype}{multi} -> {cell_name:2s} : {colour}{r}{RESET}")
    print(f"\n  * multi-cell machining recipe")

    # -- Timing report --------------------------------------------------------
    if measured_times:
        hdr("Timing Report")
        print(f"  {'Product':<8} {'Cell':<4} {'Times':<20} "
              f"{'Avg':>6} {'Estimate':>10} {'Diff':>7}")
        print(f"  {'-'*8} {'-'*4} {'-'*20} {'-'*6} {'-'*10} {'-'*7}")
        for (ptype, cell_name), times in sorted(measured_times.items()):
            avg  = sum(times) / len(times)
            est  = CURRENT_ESTIMATES.get(ptype, 0)
            diff = avg - est
            colour = (RED    if abs(diff) > 10 else
                      YELLOW if abs(diff) > 5  else GREEN)
            print(f"  {ptype:<8} {cell_name:<4} "
                  f"{' '.join(f'{t}s' for t in times):<20} "
                  f"{avg:5.0f}s  {est:7d}s est  "
                  f"{colour}{diff:+.0f}s{RESET}")

        print(f"\n  {BOLD}Suggested ESTIMATED_TIME updates:{RESET}")
        updated = {}
        for (ptype, _), times in measured_times.items():
            updated.setdefault(ptype, round(sum(times) / len(times)))
        print("  ESTIMATED_TIME = {")
        for ptype in PRODUCT_TYPES:
            if ptype in updated:
                old  = CURRENT_ESTIMATES[ptype]
                new  = updated[ptype]
                note = f"  # was {old}s" if new != old else ""
                print(f'    "{ptype}": {new},{note}')
            else:
                print(f'    "{ptype}": {CURRENT_ESTIMATES[ptype]},  '
                      f'# not tested')
        print("  }")

    save_results(results, auto, use_mes, repeat)
    h.disconnect()
    print()


if __name__ == "__main__":
    main()
