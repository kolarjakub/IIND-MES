"""
main.py
=======
Entry point — starts MES and ERP.

Usage:
    python main.py          # normal mode: MES + ERP + suppliers
    python main.py --test   # test mode: MES only, no ERP, inject materials directly
"""

import threading
import time
import argparse
from colorama import init, Fore, Style

init(autoreset=True)


def run_normal():
    """Full production mode — MES + ERP + supplier logic."""
    from mes import MES
    from erp import ERP

    print(f"{Fore.GREEN}[MAIN] Starting — normal mode (MES + ERP){Style.RESET_ALL}")

    mes = MES()
    erp = ERP(mes)
    mes.set_erp(erp)

    mes_thread = threading.Thread(target=mes.run, daemon=True, name="mes")
    mes_thread.start()

    print(f"{Fore.YELLOW}[MAIN] Waiting for MES to start...{Style.RESET_ALL}")
    time.sleep(4)

    erp.run()


def run_test():
    """
    Test mode — MES only, no ERP, no supplier scheme.

    Lets you inject materials directly and send orders without
    waiting for day ticks, supplier deliveries, or penalty tracking.
    Useful for testing recipes, scheduling, and PLC integration.
    """
    from mes import MES
    from orders import ClientOrder, Order

    print(f"{Fore.GREEN}[MAIN] Starting — TEST MODE (MES only, no ERP){Style.RESET_ALL}")
    print(f"{Fore.YELLOW}[MAIN] No supplier logic, no day ticks, no penalties{Style.RESET_ALL}")

    mes = MES()

    mes_thread = threading.Thread(target=mes.run, daemon=True, name="mes")
    mes_thread.start()

    print(f"{Fore.YELLOW}[MAIN] Waiting for MES to connect...{Style.RESET_ALL}")
    time.sleep(4)

    # Interactive test shell
    print(f"\n{Fore.CYAN}═══════════════════════════════════════{Style.RESET_ALL}")
    print(f"{Fore.CYAN}  MES Test Shell — commands:{Style.RESET_ALL}")
    print(f"  wood <n>           — add n Wood to W1")
    print(f"  metal <n>          — add n Metal to W1")
    print(f"  order <type> <qty> — queue an order (e.g. order RWW 3)")
    print(f"  status             — show MES status")
    print(f"  q / exit           — quit")
    print(f"{Fore.CYAN}═══════════════════════════════════════{Style.RESET_ALL}\n")

    order_id = 1

    try:
        while True:
            cmd = input(f"{Fore.CYAN}test> {Style.RESET_ALL}").strip()
            if not cmd:
                continue

            parts = cmd.split()
            verb  = parts[0].lower()

            if verb in ("q", "exit", "quit"):
                break

            elif verb == "wood":
                n = int(parts[1]) if len(parts) > 1 else 6
                mes.add_materials(wood=n, metal=0)

            elif verb == "metal":
                n = int(parts[1]) if len(parts) > 1 else 6
                mes.add_materials(wood=0, metal=n)

            elif verb == "order":
                if len(parts) < 3:
                    print("Usage: order <type> <qty>  e.g. order RWW 3")
                    continue
                ptype = parts[1].upper()
                qty   = int(parts[2])
                ddate = int(parts[3]) if len(parts) > 3 else 10
                pen   = int(parts[4]) if len(parts) > 4 else 100

                client_order = ClientOrder(
                    name    = "TestClient",
                    NIF     = 0,
                    OrderID = order_id,
                    orders  = [Order(type=ptype, quantity=qty,
                                     DDate=ddate, Penalty=pen)]
                )
                mes.receive_order(client_order)
                print(f"{Fore.GREEN}[TEST] Order {order_id}: {qty}x {ptype}{Style.RESET_ALL}")
                order_id += 1

            elif verb == "status":
                s = mes.get_status()
                print(f"  W1  : Wood={s['warehouse_W1']['wood']} "
                      f"Metal={s['warehouse_W1']['metal']}")
                print(f"  W2  : {s['warehouse_W2']['wood']} finished")
                print(f"  Orders: Pending={s['pending']} "
                      f"InProgress={s['in_progress']} "
                      f"Completed={s['completed']}")
                print(f"  PLC ready: {s['plc_ready']}")

                if s['pending_orders']:
                    print(f"  Pending:")
                    for o in s['pending_orders']:
                        print(f"    {o['quantity']}x {o['piece_type']}")
                if s['in_progress_orders']:
                    print(f"  In progress:")
                    for o in s['in_progress_orders']:
                        print(f"    {o['quantity']}x {o['piece_type']} "
                              f"→ dock {o['dock']}")
            else:
                print(f"Unknown command: {verb}")
                print(f"Commands: wood, metal, order, status, q")

    except KeyboardInterrupt:
        pass

    print(f"\n{Fore.YELLOW}[MAIN] Test session ended.{Style.RESET_ALL}")
    mes._plc.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true",
                        help="Test mode — MES only, no ERP")
    args = parser.parse_args()

    if args.test:
        run_test()
    else:
        run_normal()