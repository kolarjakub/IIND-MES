"""
main.py
=======
Entry point for the Flexible Production Line MES.

Starts the MES scheduler in a background thread, then opens an
interactive console so you can monitor production and inject orders
while the factory is running.

Usage
-----
    python main.py                              # defaults (local CODESYS)
    python main.py --plc opc.tcp://192.168.1.5:4840
    python main.py --port 7000 --cap 15

Interactive commands (once running)
------------------------------------
    status   (s)   -- production stats + current queue
    queue    (q)   -- live priority queue
    add      (a)   -- add a production order interactively
    day            -- trigger an immediate unload cycle (for testing)
    help     (?)   -- show this list
    exit           -- stop MES and quit
"""

import argparse
import getpass
import sys
import threading
import time

try:
    from plc_interface import PLCInterface
except ImportError:
    print("ERROR: plc_interface.py not found -- make sure it is in the same folder.")
    sys.exit(1)

try:
    from mes import (
        MES, _banner, _log,
        ORDER_HOST, ORDER_PORT,
        WAREHOUSE_CAP, UNLOAD_INTERVAL,
    )
except ImportError as e:
    print(f"ERROR: mes.py not found or has an error:\n  {e}")
    sys.exit(1)

try:
    from orders import VALID_TYPES, ClientOrder, Order as ProdOrder
except ImportError:
    print("ERROR: orders.py not found.")
    sys.exit(1)

# ── ANSI colours ──────────────────────────────────────────────────────────────
try:
    import colorama; colorama.init()
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    RED    = "\033[91m"
    CYAN   = "\033[96m"
    BOLD   = "\033[1m"
    RESET  = "\033[0m"
except ImportError:
    GREEN = YELLOW = RED = CYAN = BOLD = RESET = ""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _print_banner():
    print(f"""
{BOLD}{CYAN}
  ╔══════════════════════════════════════════════════════════╗
  ║        Flexible Production Line  ·  MES Console         ║
  ╚══════════════════════════════════════════════════════════╝
{RESET}
  Type {BOLD}help{RESET} for available commands.
  Orders arrive automatically on TCP port {ORDER_PORT}.
  Press {BOLD}Ctrl+C{RESET} or type {BOLD}exit{RESET} to stop.
""")


def _print_help():
    print(f"""
  {BOLD}Commands{RESET}
  ─────────────────────────────────────────────────────────
  {CYAN}status{RESET}  (s)   Production stats + current queue
  {CYAN}queue{RESET}   (q)   Live priority queue
  {CYAN}add{RESET}     (a)   Inject a production order now
  {CYAN}day{RESET}           Force an immediate unload cycle
  {CYAN}help{RESET}    (?)   Show this help
  {CYAN}exit{RESET}          Stop MES and quit
  ─────────────────────────────────────────────────────────
""")


def _add_order_interactive(mes: MES):
    """
    Prompt the user for order fields, build a ClientOrder and inject
    it into the MES via the same on_client_order() path that TCP uses.
    """
    print(f"\n  {BOLD}Add production order{RESET}")
    print(f"  Valid types: {', '.join(VALID_TYPES)}\n")
    try:
        client  = input("    Client name       : ").strip() or "Console"
        nif_s   = input("    NIF (9 digits)    : ").strip()
        nif     = int(nif_s) if nif_s.isdigit() else 0
        ref_s   = input("    OrderID (1-1000)  : ").strip()
        ref     = int(ref_s) if ref_s.isdigit() else 1

        ptype   = input(f"    Type              : ").strip().upper()
        if ptype not in VALID_TYPES:
            print(f"  {RED}Unknown type '{ptype}'.  Valid: {VALID_TYPES}{RESET}")
            return

        qty_s   = input("    Quantity          : ").strip()
        qty     = int(qty_s) if qty_s.isdigit() and int(qty_s) > 0 else 0
        if qty <= 0:
            print(f"  {RED}Quantity must be a positive integer.{RESET}")
            return

        ddate_s = input("    DDate (days)      : ").strip()
        ddate   = int(ddate_s) if ddate_s.isdigit() and int(ddate_s) > 0 else 0
        if ddate <= 0:
            print(f"  {RED}DDate must be >= 1.{RESET}")
            return

        pen_s   = input("    Penalty (int €/day): ").strip()
        pen     = int(pen_s) if pen_s.lstrip('-').isdigit() else 0
        if pen < 0:
            print(f"  {RED}Penalty cannot be negative.{RESET}")
            return

    except (KeyboardInterrupt, EOFError):
        print("\n  Cancelled.")
        return
    except ValueError as e:
        print(f"  {RED}Invalid input: {e}{RESET}")
        return

    # Build ClientOrder using the same dataclasses the generator uses
    order_item   = ProdOrder(type=ptype, quantity=qty, DDate=ddate, Penalty=pen)
    client_order = ClientOrder(name=client, NIF=nif, OrderID=ref,
                               orders=[order_item])
    mes.on_client_order(client_order)
    print(f"\n  {GREEN}✓ Injected: {qty}×{ptype}  "
          f"DDate={ddate}d  Penalty={pen}€/day{RESET}\n")


# ── Interactive console ───────────────────────────────────────────────────────

def _run_console(mes: MES):
    _print_help()

    ALIASES = {
        "s": "status", "q": "queue", "a": "add",
        "?": "help",   "h": "help",  "quit": "exit",
    }

    while True:
        try:
            raw = input(f"  {BOLD}mes>{RESET} ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            raw = "exit"

        cmd = ALIASES.get(raw, raw)

        if   cmd == "status": mes.print_stats()
        elif cmd == "queue":  mes._print_queue(); print()
        elif cmd == "add":    _add_order_interactive(mes)
        elif cmd == "day":
            print(f"\n  {YELLOW}Forcing unload cycle...{RESET}")
            mes._do_unload(); print()
        elif cmd == "help":   _print_help()
        elif cmd == "exit":
            print(f"\n  {YELLOW}Stopping MES...{RESET}")
            break
        elif cmd == "":
            pass
        else:
            print(f"  Unknown command '{raw}'.  "
                  f"Type {BOLD}help{RESET} for options.\n")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    global WAREHOUSE_CAP, UNLOAD_INTERVAL

    ap = argparse.ArgumentParser(
        description="Flexible Line MES -- interactive launcher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples
  python main.py
  python main.py --plc opc.tcp://192.168.1.5:4840
  python main.py --port 7000 --cap 15 --day 30
""")
    ap.add_argument("--plc",  default="opc.tcp://127.0.0.1:4840", metavar="URL",
                    help="OPC-UA server URL")
    ap.add_argument("--user", default=None, metavar="USER",
                    help="OPC-UA username (omit for anonymous)")
    ap.add_argument("--port", type=int, default=ORDER_PORT, metavar="PORT",
                    help=f"TCP order receiver port (default {ORDER_PORT})")
    ap.add_argument("--bind", default=ORDER_HOST, metavar="HOST",
                    help="Bind address for receiver (default 0.0.0.0)")
    ap.add_argument("--cap",  type=int, default=WAREHOUSE_CAP, metavar="N",
                    help=f"Max pieces per day (default {WAREHOUSE_CAP})")
    ap.add_argument("--day",  type=float, default=UNLOAD_INTERVAL, metavar="S",
                    help=f"Day length in seconds (default {UNLOAD_INTERVAL})")
    ap.add_argument("--verbose", action="store_true",
                    help="Enable debug logging")
    args = ap.parse_args()

    import mes as _mes_module
    _mes_module.WAREHOUSE_CAP   = args.cap
    _mes_module.UNLOAD_INTERVAL = args.day

    _print_banner()
    print(f"  {BOLD}Configuration{RESET}")
    print(f"    PLC (OPC-UA)    : {args.plc}")
    print(f"    Order receiver  : {args.bind}:{args.port}")
    print(f"    Day cap         : {args.cap} pieces")
    print(f"    Day length      : {args.day}s")
    print()

    if args.verbose:
        import logging
        logging.basicConfig(level=logging.DEBUG,
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    password = None
    if args.user:
        password = getpass.getpass(f"  OPC-UA password for '{args.user}': ")

    print(f"  Connecting to PLC at {args.plc} ...", end=" ", flush=True)
    plc = PLCInterface(server_url=args.plc, username=args.user, password=password)
    if not plc.connect():
        print(f"\n\n  {RED}ERROR: Cannot connect to CODESYS.{RESET}")
        print("  Make sure CODESYS / SFS is running and the URL is correct.")
        sys.exit(1)

    try:
        status = plc.get_status()
        wh     = status["warehouse"]
        print(f"{GREEN}OK{RESET}")
        print(f"    ready={status['ready']}  "
              f"W1={wh['W1']} (wood={wh.get('W1_wood',0)} "
              f"metal={wh.get('W1_metal',0)})  "
              f"W2={wh['W2']}")
    except Exception as e:
        print(f"\n  {YELLOW}Warning: could not read PLC status: {e}{RESET}")

    # Create MES, start receiver + scheduler
    mes = MES(plc, order_host=args.bind, order_port=args.port)
    mes.start_receiver()

    scheduler = threading.Thread(target=mes.run, daemon=True, name="mes-scheduler")
    scheduler.start()
    _log("Scheduler thread started")

    try:
        _run_console(mes)
    except Exception as e:
        print(f"\n  {RED}Console error: {e}{RESET}")

    _banner("Shutting down")
    mes.stop()
    scheduler.join(timeout=5.0)
    plc.disconnect()
    mes.print_stats()
    _log("MES stopped.")
    print()


if __name__ == "__main__":
    main()
