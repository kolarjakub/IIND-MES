"""
test_multiple_orders.py
=======================
Test sending multiple orders to MES and watching how they get processed.

This test:
1. Starts MES in a background thread
2. Seeds W1 with enough materials for all orders
3. Sends multiple orders via TCP
4. Polls MES status every second and prints a live table

Run:
    python test_multiple_orders.py

Make sure CODESYS and SFS are running before starting.
"""

import threading
import time
import sys

# ── Colour helpers ────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
YELLOW = "\033[93m"
CYAN   = "\033[94m"
RED    = "\033[91m"
RESET  = "\033[0m"

def section(title):
    print(f"\n{CYAN}{'='*55}{RESET}")
    print(f"  {title}")
    print(f"{CYAN}{'='*55}{RESET}")


# ── Test orders ───────────────────────────────────────────────────────────────
# Define what orders to send and how much material they need
TEST_ORDERS = [
    {"type": "RWW", "quantity": 1, "DDate": 5,  "Penalty": 200},  # needs 3 Wood
    {"type": "SWW", "quantity": 1, "DDate": 3,  "Penalty": 500},  # needs 3 Wood, urgent
    {"type": "RWW", "quantity": 1, "DDate": 10, "Penalty": 100},  # needs 3 Wood, low priority
]

# Total materials needed
TOTAL_WOOD  = sum(3 for o in TEST_ORDERS if o["type"] in ["RWW", "SWW", "RWM", "SWM"])
TOTAL_METAL = sum(3 for o in TEST_ORDERS if o["type"] in ["RMM", "SMM"])


def run_mes(mes):
    """Run MES in background — catches exceptions so test can continue."""
    try:
        mes.run()
    except Exception as e:
        print(f"{RED}[MES ERROR] {e}{RESET}")


def send_test_orders():
    """Send all test orders via TCP to port 6666."""
    from order_generator import send_client_order
    from orders import ClientOrder, Order

    time.sleep(2)  # wait for MES receiver to start

    for i, o in enumerate(TEST_ORDERS):
        order = ClientOrder(
            name    = f"Test ERP Client {i+1}",
            NIF     = 100000000 + i,
            OrderID = i + 1,
            orders  = [Order(
                type     = o["type"],
                quantity = o["quantity"],
                DDate    = o["DDate"],
                Penalty  = o["Penalty"],
            )]
        )
        send_client_order(order)
        print(f"{GREEN}[TEST] Sent order {i+1}: "
              f"{o['quantity']}x {o['type']} "
              f"DDate={o['DDate']} Penalty={o['Penalty']}{RESET}")
        time.sleep(0.5)  # small gap between orders


def print_status(mes, elapsed):
    """Print a one-line status update."""
    status = mes.get_status()
    w1     = status["warehouse_W1"]
    print(
        f"  [{elapsed:4d}s] "
        f"W1=Wood:{w1['wood']} Metal:{w1['metal']} | "
        f"Pending={status['pending']} "
        f"InProgress={status['in_progress']} "
        f"Completed={status['completed']} | "
        f"PLC={'ready' if status['plc_ready'] else 'busy'}"
    )


def main():
    section("Multiple Orders Test")
    print(f"  Orders to send: {len(TEST_ORDERS)}")
    print(f"  Total Wood needed : {TOTAL_WOOD}")
    print(f"  Total Metal needed: {TOTAL_METAL}")

    # ── Start MES ─────────────────────────────────────────────────────────────
    section("Starting MES")
    from mes import MES
    mes = MES()

    # Seed W1 with enough materials for all orders
    mes.add_materials(wood=TOTAL_WOOD, metal=TOTAL_METAL)
    print(f"{GREEN}[TEST] Seeded W1: Wood={TOTAL_WOOD} Metal={TOTAL_METAL}{RESET}")

    # Run MES in background thread
    mes_thread = threading.Thread(target=run_mes, args=(mes,), daemon=True)
    mes_thread.start()
    print(f"{GREEN}[TEST] MES thread started{RESET}")


    # Wait for MES to fully start (connect to PLC, sync warehouse)
    time.sleep(3)  # ← give it time to sync

    # Seed W1 AFTER sync so it doesn't get wiped
    mes.add_materials(wood=TOTAL_WOOD, metal=TOTAL_METAL)
    print(f"{GREEN}[TEST] Seeded W1: Wood={TOTAL_WOOD} Metal={TOTAL_METAL}{RESET}")

    # ── Send orders ───────────────────────────────────────────────────────────
    section("Sending Orders")
    order_thread = threading.Thread(target=send_test_orders, daemon=True)
    order_thread.start()

    # ── Poll status ───────────────────────────────────────────────────────────
    section("Monitoring (watching for 120s)")
    print(f"  Watch SFS for piece movement!\n")

    start    = time.time()
    timeout  = 120
    prev_completed = 0

    for i in range(timeout):
        time.sleep(1)
        elapsed = int(time.time() - start)

        try:
            print_status(mes, elapsed)
            status = mes.get_status()

            # Highlight when an order completes
            if status["completed"] > prev_completed:
                print(f"{GREEN}  *** ORDER COMPLETED! "
                      f"Total completed: {status['completed']}/{len(TEST_ORDERS)} ***"
                      f"{RESET}")
                prev_completed = status["completed"]

            # All done
            if status["completed"] == len(TEST_ORDERS):
                print(f"\n{GREEN}ALL {len(TEST_ORDERS)} ORDERS COMPLETED "
                      f"in {elapsed}s!{RESET}")
                break

        except Exception as e:
            print(f"{RED}[TEST] Status read error: {e}{RESET}")

    else:
        print(f"\n{YELLOW}Timeout after {timeout}s. "
              f"Completed {prev_completed}/{len(TEST_ORDERS)} orders.{RESET}")
        print(f"{YELLOW}Check: is CODESYS running? Are pieces moving in SFS?{RESET}")

    section("Summary")
    try:
        status = mes.get_status()
        print(f"  Pending    : {status['pending']}")
        print(f"  In Progress: {status['in_progress']}")
        print(f"  Completed  : {status['completed']}")
        print(f"  W1 remaining: Wood={status['warehouse_W1']['wood']} "
              f"Metal={status['warehouse_W1']['metal']}")
    except Exception:
        pass

    print(f"\n{CYAN}Test finished. Press Ctrl+C to exit.{RESET}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nDone.")


if __name__ == "__main__":
    main()