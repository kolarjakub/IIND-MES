#!/usr/bin/env python3
"""
dashboard.py
============
Live terminal dashboard for the Flexible Production Line MES.
Satisfies requirements §4.2 (User Interface) and §4.3 (Statistics).

Panels
------
  • Warehouse status   — W1 raw stock (wood/metal), W2 finished
  • Production queue   — orders with priority score, progress bar, status
  • Machine statistics — occupation %, operating time, tool times (per spec
                         Table 1), tool changes, pieces total
  • Active procedures  — what the PLC is currently running + any errors
  • Unloaded pieces    — per dock, per type (from DB)

Usage
-----
    # standalone — own PLC connection:
    python dashboard.py
    python dashboard.py --plc opc.tcp://192.168.1.5:4840
    python dashboard.py --refresh 5
    python dashboard.py --no-plc          # DB-only, no machine stats

    # from main.py console:
    mes> dashboard                         # press Ctrl+C to return to console

Requirements
------------
    pip install rich
"""

import argparse
import time
import sys
from datetime import datetime

try:
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live
    from rich.table import Table
    from rich.panel import Panel
    from rich import box
except ImportError:
    print("ERROR: 'rich' not installed.\n  Run:  pip install rich")
    sys.exit(1)

console = Console()

# ── constants from project spec (Table 1) ────────────────────────────────────

# Machine index → name (matches _MACHINE_NAMES in mes.py)
_MACHINE_NAMES = [
    "M1a","M1b","M1c",
    "M2a","M2b","M2c",
    "M3a","M3b","M3c",
    "M4a","M4b","M4c",
]

# Per-machine tool names for tool_times[0..2] (from spec Table 1)
_MACHINE_TOOLS = {
    "M1a": ["T1","T2","T3"],  "M1b": ["T1","T2","T3"],  "M1c": ["T8","T9","T11"],
    "M2a": ["T1","T2","T3"],  "M2b": ["T1","T2","T3"],  "M2c": ["T8","T9","T10"],
    "M3a": ["T4","T5","T6"],  "M3b": ["T4","T5","T6"],  "M3c": ["T8","T9","T11"],
    "M4a": ["T4","T5","T6"],  "M4b": ["T4","T5","T6"],  "M4c": ["T8","T9","T10"],
}

# EPieceType index → short name (for pieces_by_type display)
_PIECE_NAMES = {
    1:"Wood",2:"Metal",3:"RtopW",4:"STopW",5:"LegW",
    6:"RtopM",7:"STopM",8:"LegM",
    9:"RWW",10:"SWW",11:"RWM",12:"SWM",13:"RMM",14:"SMM",
}

_STATUS_COL = {
    "PENDING":"yellow","IN_PROGRESS":"cyan",
    "COMPLETED":"green","FAILED":"red",
}

def _c(text, colour):
    return f"[{colour}]{text}[/{colour}]"

def _bar(done, total, width=10):
    if not total:
        return "░" * width
    filled = int(done / total * width)
    return "█" * filled + "░" * (width - filled)


# ── data helpers ──────────────────────────────────────────────────────────────

def _orders_from_mes(mes):
    with mes._lock:
        return list(mes._orders)

def _orders_from_db():
    try:
        from db_handler import load_active_orders
        return load_active_orders()
    except Exception:
        return []

def _unload_stats():
    try:
        from db_handler import db_connect, db_disconnect
        cur, conn = db_connect()
        if cur is None:
            return []
        cur.execute("""
            SELECT dock_id, piece_type, total_count
            FROM mes.unload_stats
            ORDER BY dock_id, piece_type;
        """)
        rows = cur.fetchall()
        db_disconnect(conn)
        return rows
    except Exception:
        return []


# ── panel builders ─────────────────────────────────────────────────────────────

def _make_header(plc) -> Panel:
    wh = {}
    if plc:
        try:
            wh = plc.get_warehouse_status()
        except Exception:
            pass
    w1    = wh.get("W1",           "─")
    wood  = wh.get("W1_wood",      "─")
    metal = wh.get("W1_metal",     "─")
    w2    = wh.get("W2",           "─")
    fin   = wh.get("W2_finished",  "─")
    ts    = datetime.now().strftime("%H:%M:%S")

    txt = (
        f"[bold cyan]Flexible Production Line  ·  MES Dashboard[/bold cyan]"
        f"   [dim]{ts}[/dim]"
        f"     W1 [yellow]{w1:>2}[/yellow]"
        f"  (wood [green]{wood}[/green]"
        f"  metal [blue]{metal}[/blue])"
        f"     W2 [magenta]{w2:>2}[/magenta]"
        f"  (finished [green]{fin}[/green])"
    )
    return Panel(txt, box=box.HORIZONTALS, style="on grey11")


def _make_orders(mes) -> Table:
    t = Table(
        title="[bold]Production Queue[/bold]",
        box=box.SIMPLE_HEAVY, expand=True, show_lines=False,
    )
    t.add_column("#",       style="dim", width=4)
    t.add_column("Type",    width=5)
    t.add_column("Progress",width=20)
    t.add_column("Score",   width=7,  justify="right")
    t.add_column("Penalty", width=9,  justify="right")
    t.add_column("DDate",   width=6,  justify="right")
    t.add_column("Status",  width=12)

    if mes is not None:
        orders = _orders_from_mes(mes)
        for o in orders[:14]:
            col  = _STATUS_COL.get(o.status, "white")
            bar  = _bar(o.quantity_done, o.quantity)
            prog = f"{o.quantity_done}/{o.quantity}  [{col}]{bar}[/{col}]"
            t.add_row(
                str(o.db_order_id or "?"),
                f"[bold]{o.piece_type}[/bold]",
                prog,
                f"{o.priority:.3f}",
                f"€{o.penalty}",
                f"{o.ddate_days}d",
                _c(o.status, col),
            )
    else:
        orders = _orders_from_db()
        for o in orders[:14]:
            done  = o.get("quantity_done", 0)
            qty   = o.get("quantity", 0)
            ptype = o.get("type", "?")
            st    = o.get("status", "?")
            col   = _STATUS_COL.get(st, "white")
            ddate = o.get("DDate") or o.get("ddate") or "?"
            bar   = _bar(done, qty)
            prog  = f"{done}/{qty}  [{col}]{bar}[/{col}]"
            t.add_row(
                str(o.get("order_id", "?")),
                f"[bold]{ptype}[/bold]",
                prog, "─",
                f"€{o.get('penalty', 0)}",
                f"{ddate}d",
                _c(st, col),
            )

    if t.row_count == 0:
        t.add_row("─", "[dim]No active orders[/dim]", "", "", "", "", "")
    return t


def _make_machines(plc) -> Table:
    t = Table(
        title="[bold]Machine Statistics[/bold]",
        box=box.SIMPLE_HEAVY, expand=True, show_lines=False,
    )
    t.add_column("Machine", width=8)
    t.add_column("Occ%",    width=7,  justify="right")
    t.add_column("OpTime",  width=9,  justify="right")
    t.add_column("Chg",     width=5,  justify="right")
    t.add_column("Pieces",  width=7,  justify="right")
    # Tool time columns — labelled per actual tool name
    t.add_column("Tool-1",  width=9,  justify="right")
    t.add_column("Tool-2",  width=9,  justify="right")
    t.add_column("Tool-3",  width=9,  justify="right")

    machines = []
    if plc:
        try:
            machines = plc.get_machine_statistics()
        except Exception:
            pass

    for m in machines:
        i = m["machine_index"]
        if i >= len(_MACHINE_NAMES):
            continue
        name  = _MACHINE_NAMES[i]
        tools = _MACHINE_TOOLS.get(name, ["T?","T?","T?"])
        occ   = m["occupation_pct"]
        op    = m["operating_time"]
        chg   = m["tool_changes"]
        tot   = m["pieces_total"]
        tt    = m["tool_times"]
        occ_c = "green" if occ > 60 else ("yellow" if occ > 25 else "dim")
        t.add_row(
            f"[bold]{name}[/bold]",
            _c(f"{occ:.1f}%", occ_c),
            f"{op:.0f}s",
            str(chg),
            str(tot),
            f"[dim]{tools[0]}[/dim] {tt[0]:.0f}s",
            f"[dim]{tools[1]}[/dim] {tt[1]:.0f}s",
            f"[dim]{tools[2]}[/dim] {tt[2]:.0f}s",
        )

    if not machines:
        t.add_row("─", "[dim]No PLC data[/dim]", "", "", "", "", "", "")
    return t


def _make_procedures(plc) -> Panel:
    STATUS_MAP = {0:"IDLE",1:"NEW ",2:"EXEC",3:"STOP",4:"DONE"}
    lines = []
    if plc:
        try:
            for p in plc.get_procedures()[:8]:
                st  = STATUS_MAP.get(p["status"], str(p["status"]))
                col = "green" if st.strip() == "EXEC" else "yellow"
                lines.append(f"  proc [cyan]{p['id']:>6}[/cyan]  [{col}]{st}[/{col}]")
            for e in plc.get_errors()[:4]:
                lines.append(
                    f"  [red bold]ERR {e['code']}[/red bold]"
                    f"  proc={e['procedure_id']}  slot={e['slot']}"
                )
        except Exception as ex:
            lines.append(f"  [red]Read error: {ex}[/red]")
    if not lines:
        lines.append("  [dim]No active procedures[/dim]")
    return Panel(
        "\n".join(lines),
        title="[bold]Active Procedures / Errors[/bold]",
        box=box.SIMPLE_HEAVY, padding=(0, 1),
    )


def _make_unload() -> Table:
    t = Table(
        title="[bold]Unloaded Work-pieces[/bold]",
        box=box.SIMPLE_HEAVY, expand=True, show_lines=False,
    )
    t.add_column("Dock",       width=6,  justify="center")
    t.add_column("Type",       width=6)
    t.add_column("Count",      width=7,  justify="right")
    t.add_column("Dock Total", width=11, justify="right")

    rows = _unload_stats()
    dock_totals: dict = {}
    for dock, ptype, cnt in rows:
        dock_totals[dock] = dock_totals.get(dock, 0) + cnt

    for dock, ptype, cnt in rows:
        t.add_row(
            str(dock),
            ptype or "?",
            str(cnt),
            str(dock_totals.get(dock, 0)),
        )
    if t.row_count == 0:
        t.add_row("─", "[dim]No data[/dim]", "─", "─")
    return t


# ── layout ─────────────────────────────────────────────────────────────────────

def _build(mes=None, plc=None) -> Layout:
    root = Layout()
    root.split_column(
        Layout(name="header",  size=3),
        Layout(name="middle",  ratio=3),
        Layout(name="bottom",  ratio=2),
    )
    root["middle"].split_row(
        Layout(name="orders",   ratio=4),
        Layout(name="machines", ratio=6),
    )
    root["bottom"].split_row(
        Layout(name="procs",  ratio=3),
        Layout(name="unload", ratio=2),
    )
    root["header"].update(_make_header(plc))
    root["orders"].update(Panel(_make_orders(mes),    box=box.SIMPLE, padding=0))
    root["machines"].update(Panel(_make_machines(plc), box=box.SIMPLE, padding=0))
    root["procs"].update(_make_procedures(plc))
    root["unload"].update(Panel(_make_unload(),       box=box.SIMPLE, padding=0))
    return root


# ── public API ─────────────────────────────────────────────────────────────────

def run_dashboard(mes=None, plc=None, refresh: float = 3.0):
    """
    Start the live dashboard.  Blocks until Ctrl+C.

    Args:
        mes:     MES instance — live in-memory order data.
                 If None, orders are read from the database.
        plc:     PLCInterface — live machine stats, procedures, warehouse.
                 If None, those sections show "No PLC data".
        refresh: seconds between redraws.
    """
    with Live(_build(mes, plc), console=console,
              refresh_per_second=1, screen=True) as live:
        try:
            while True:
                live.update(_build(mes, plc))
                time.sleep(refresh)
        except KeyboardInterrupt:
            pass


# ── standalone entry point ──────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="MES Live Terminal Dashboard",
        epilog="  Press Ctrl+C to exit.",
    )
    ap.add_argument("--plc",     default="opc.tcp://127.0.0.1:4840", metavar="URL")
    ap.add_argument("--user",    default=None,  metavar="USER",
                    help="OPC-UA username (anonymous if omitted)")
    ap.add_argument("--refresh", type=float, default=3.0, metavar="S",
                    help="Refresh interval in seconds (default 3)")
    ap.add_argument("--no-plc",  action="store_true",
                    help="Skip PLC connection — show only DB data")
    args = ap.parse_args()

    plc = None
    if not args.no_plc:
        try:
            from plc_interface import PLCInterface
            pw = None
            if args.user:
                import getpass
                pw = getpass.getpass(
                    f"  OPC-UA password for '{args.user}' (Enter=anonymous): "
                ).strip() or None
            plc = PLCInterface(server_url=args.plc, username=args.user, password=pw)
            if plc.connect():
                console.print(f"[green]PLC connected: {args.plc}[/green]")
                time.sleep(0.4)
            else:
                console.print("[yellow]PLC connection failed — DB-only mode[/yellow]")
                plc = None
        except Exception as ex:
            console.print(f"[yellow]PLC error ({ex}) — DB-only mode[/yellow]")
            plc = None

    run_dashboard(plc=plc, refresh=args.refresh)
