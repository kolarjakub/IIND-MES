"""
test_e2e.py
===========
End-to-end test: send a recipe to the PLC and watch SFS produce a table.

PREREQUISITES:
  1. CODESYS is running with Project PLC (v4)
  2. SFS simulator is running and connected to CODESYS via Modbus
  3. Wood pieces are available on the SFS loading docks (Cell L)
     — OR — you add them during the test when prompted

Run:
  python test_e2e.py           # test RWW (Wood Round Table)
  python test_e2e.py --type SWW
  python test_e2e.py --offline  # offline recipe builder test only
"""

import sys
import time
import argparse

# ── Colour helpers ────────────────────────────────────────────────────────────

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
BLUE   = "\033[94m"
RESET  = "\033[0m"

def ok(msg):   print(f"  {GREEN}[PASS]{RESET} {msg}")
def fail(msg): print(f"  {RED}[FAIL]{RESET} {msg}")
def info(msg): print(f"  {BLUE}[INFO]{RESET} {msg}")
def warn(msg): print(f"  {YELLOW}[WARN]{RESET} {msg}")

def section(title):
    print(f"\n{BLUE}{'='*55}{RESET}")
    print(f"  {title}")
    print(f"{BLUE}{'='*55}{RESET}")


# ── Offline tests (no PLC needed) ─────────────────────────────────────────────

def test_recipe_builder():
    section("Recipe builder test (no PLC needed)")
    from opcua_handler import build_recipe, ELocation, PRODUCT_RECIPES

    all_ok = True
    for ptype in PRODUCT_RECIPES:
        slots = build_recipe(
            piece_type="RWW",
            id_recipe=10,
            id_procedure_start=101,
            id_piece_start=60001,
            id_final_piece=60100,
        )

        if len(slots) != 13:
            fail(f"{ptype}: got {len(slots)} slots, expected 13")
            all_ok = False
            continue

        procs = [s["ID_Procedure"] for s in slots]
        if procs != list(range(101, 114)):
            fail(f"{ptype}: ID_Procedure not sequential: {procs}")
            all_ok = False
            continue

        load_ok  = all(s["Cell"] == ELocation.L for s in slots[0:3])
        trans_ok = all(s["Cell"] == ELocation.T for s in slots[6:9])
        asm_ok   = all(s["For_Assembly"] == True for s in slots[9:12])
        unload_ok= slots[12]["Cell"] == ELocation.U

        leg1 = slots[9]["IDs_Assembly_Leg_1"]
        leg2 = slots[10]["IDs_Assembly_Leg_2"]
        top  = slots[11]["IDs_Assembly_Top"]
        ids_ok = leg1 > 0 and leg2 > 0 and top > 0

        if load_ok and trans_ok and asm_ok and unload_ok and ids_ok:
            ok(f"{ptype}: phases OK | Leg1={leg1} Leg2={leg2} Top={top}")
        else:
            fail(f"{ptype}: load={load_ok} trans={trans_ok} "
                 f"asm={asm_ok} unload={unload_ok} ids={ids_ok}")
            all_ok = False

    return all_ok


def test_static_helpers():
    section("PLCInterface static helpers (no PLC needed)")
    from plc_interface import PLCInterface

    all_ok = True

    # Estimate times
    expected = {"RWW": 60, "SWW": 50, "RWM": 100, "SWM": 90, "RMM": 105, "SMM": 95}
    for ptype, exp in expected.items():
        t = PLCInterface.estimate_time(ptype, 1)
        if t == exp:
            ok(f"estimate_time({ptype}, 1) = {t}s")
        else:
            fail(f"estimate_time({ptype}, 1) = {t}, expected {exp}")
            all_ok = False

    # Raw materials
    mat_cases = {
        "RWW": {"Wood": 3, "Metal": 0},
        "RMM": {"Wood": 0, "Metal": 3},
        "RWM": {"Wood": 1, "Metal": 2},
    }
    for ptype, exp in mat_cases.items():
        mat = PLCInterface.raw_materials_needed(ptype, 1)
        if mat == exp:
            ok(f"raw_materials({ptype}) = {mat}")
        else:
            fail(f"raw_materials({ptype}) = {mat}, expected {exp}")
            all_ok = False

    # Dock splitting
    dock_cases = [(6, [6]), (7, [6, 1]), (14, [6, 6, 2]), (30, [6, 6, 6, 6, 6])]
    for qty, exp in dock_cases.items() if isinstance(dock_cases, dict) else dock_cases:
        result = PLCInterface.split_into_docks(qty)
        if result == exp:
            ok(f"split_into_docks({qty}) = {result}")
        else:
            fail(f"split_into_docks({qty}) = {result}, expected {exp}")
            all_ok = False

    return all_ok


# ── Online tests (PLC required) ───────────────────────────────────────────────

def test_connection(h):
    section("OPC-UA connection")
    try:
        h.connect()
        ok("Connected to CODESYS OPC-UA server")
        return True
    except Exception as e:
        fail(f"Connection failed: {e}")
        return False


def test_reads(h):
    section("Basic reads")
    all_ok = True

    try:
        inv = h.read_warehouse_inventory()
        ok(f"Warehouse: W1={inv['W1']} W2={inv['W2']}")
    except Exception as e:
        fail(f"read_warehouse_inventory: {e}")
        all_ok = False

    try:
        n = h.read_num_errors()
        ok(f"MES_Num_Errors = {n}")
    except Exception as e:
        fail(f"read_num_errors: {e}")
        all_ok = False

    try:
        success = h.read_success()
        ok(f"MES_Success = {success}")
    except Exception as e:
        fail(f"read_success: {e}")
        all_ok = False

    try:
        procs = h.read_procedures()
        ok(f"MES_Procedures = {len(procs)} active")
    except Exception as e:
        fail(f"read_procedures: {e}")
        all_ok = False

    errors = h.read_errors()
    if errors:
        warn(f"Active errors: {errors}")
    else:
        ok("No active PLC errors")

    return all_ok


def test_dispatch_and_wait(h, piece_type, poll_seconds=120):
    section(f"Full end-to-end: produce 1x {piece_type}")

    from opcua_handler import build_recipe

    # Build recipe
    slots = build_recipe(
        piece_type          = piece_type,
        id_recipe           = 10,
        id_procedure_start  = 101,
        id_piece_start      = 60001,
        id_final_piece      = 60100,
    )
    info(f"Recipe built: {len(slots)} slots for {piece_type}")

    # Show what materials are needed
    from plc_interface import PLCInterface
    mat = PLCInterface.raw_materials_needed(piece_type, 1)
    est = PLCInterface.estimate_time(piece_type, 1)
    info(f"Needs: {mat} | Estimated time: {est}s")

    # Check warehouse before
    inv_before = h.read_warehouse_inventory()
    info(f"Warehouse before: W1={inv_before['W1']} W2={inv_before['W2']}")

    if inv_before["W1"] == 0:
        warn("W1 is empty! Add Wood/Metal pieces to SFS loading docks now.")
        warn("Waiting 15s for you to add material...")
        time.sleep(15)

    # Dispatch
    info("Dispatching recipe to PLC...")
    ok_dispatch = h.dispatch(slots)
    if ok_dispatch:
        ok("PLC acknowledged recipe")
    else:
        fail("PLC did not acknowledge recipe")
        return False

    # Poll for result
    info(f"Polling for up to {poll_seconds}s — watch SFS for movement...")
    print()

    start = time.time()
    last_inv = inv_before.copy()
    activity_seen = False

    for i in range(poll_seconds):
        time.sleep(1)
        elapsed = i + 1

        try:
            inv     = h.read_warehouse_inventory()
            procs   = h.read_procedures()
            success = h.read_success()
            errors  = h.read_errors()
        except Exception as e:
            warn(f"Read error at {elapsed}s: {e}")
            continue

        # Detect any change
        w1_changed = inv["W1"] != last_inv["W1"]
        w2_changed = inv["W2"] != last_inv["W2"]
        if w1_changed or w2_changed:
            activity_seen = True
            last_inv = inv.copy()

        # Print status line
        status = (f"  [{elapsed:4d}s] "
                  f"W1={inv['W1']:3d} W2={inv['W2']:3d} "
                  f"Procs={len(procs):2d} "
                  f"Success={success} "
                  f"Errors={len(errors)}")
        if w1_changed or w2_changed:
            print(f"{GREEN}{status} ← warehouse changed{RESET}")
        elif len(procs) > 0:
            print(f"{YELLOW}{status} ← procedures active{RESET}")
        else:
            print(status)

        if errors:
            for e in errors:
                fail(f"  PLC Error: code={e['code']} slot={e['slot']} "
                     f"proc={e['procedure_id']}")

        if success and elapsed > 2:
            print()
            ok(f"SUCCESS in {elapsed}s!")
            inv_after = h.read_warehouse_inventory()
            ok(f"Warehouse after: W1={inv_after['W1']} W2={inv_after['W2']}")
            return True

    print()
    if activity_seen:
        warn(f"Timed out after {poll_seconds}s but warehouse activity was seen")
        warn("The PLC may still be processing — check SFS")
    else:
        fail(f"Timed out after {poll_seconds}s with no activity")
        fail("Check: is SFS running? Are pieces on loading docks?")
    return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="PLC end-to-end test")
    parser.add_argument("--type",    default="RWW",
                        help="Piece type to produce (default: RWW)")
    parser.add_argument("--offline", action="store_true",
                        help="Run offline tests only (no CODESYS needed)")
    parser.add_argument("--timeout", type=int, default=120,
                        help="Polling timeout in seconds (default: 120)")
    args = parser.parse_args()

    print(f"\n{BLUE}PLC End-to-End Test{RESET}")
    print(f"Piece type: {args.type} | Timeout: {args.timeout}s")

    results = {}

    # Offline tests always run
    results["recipe_builder"] = test_recipe_builder()
    results["static_helpers"] = test_static_helpers()

    if args.offline:
        info("Offline mode — skipping OPC-UA tests")
    else:
        from opcua_handler import OpcUaHandler
        h = OpcUaHandler()

        if not test_connection(h):
            fail("Cannot connect to CODESYS — check that it is running")
            sys.exit(1)

        results["reads"]    = test_reads(h)
        results["e2e"]      = test_dispatch_and_wait(h, args.type, args.timeout)

        h.disconnect()

    # Summary
    section("Summary")
    all_passed = True
    for name, result in results.items():
        if result:
            ok(f"{name}")
        else:
            fail(f"{name}")
            all_passed = False

    print()
    if all_passed:
        print(f"{GREEN}All tests passed!{RESET}\n")
        sys.exit(0)
    else:
        print(f"{RED}Some tests failed.{RESET}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
