"""
test_db_handler.py
------------------
Sample script that exercises every function in db_handler.py with rich data.
Run with:  python test_db_handler.py
"""

import time
from db_handler import (
    db_init,
    register_machine,
    update_machine_state,
    save_to_db,
    record_tool_usage,
    snapshot_machine_stats,
    record_unload,
    log_production_start,
    log_production_end,
    get_pending_orders,
    update_order_status,
    get_tool_usage_summary,
    get_unload_summary,
    _MACHINE_TOOLS,
)


# ---------------------------------------------------------------------------
# Stub classes mimicking OrderReceiver output
# ---------------------------------------------------------------------------

class FakeOrderLine:
    def __init__(self, type_, quantity, DDate, penalty, priority=None):
        self.type     = type_
        self.quantity = quantity
        self.DDate    = DDate
        self.Penalty  = penalty
        self.priority = priority


class FakeClientOrder:
    def __init__(self, name, NIF, OrderID, orders):
        self.name    = name
        self.NIF     = NIF
        self.OrderID = OrderID
        self.orders  = orders


# ---------------------------------------------------------------------------
# 1. Init schema & register machines
#    available_tools comes directly from _MACHINE_TOOLS — same source of truth
#    as Table 1 in the project spec.
# ---------------------------------------------------------------------------

print("\n=== 1. db_init + register machines ===")
db_init()
for machine_name, (cell, tools) in _MACHINE_TOOLS.items():
    register_machine(machine_name, cell, tools)
    print(f"  {machine_name} ({cell})  available_tools={tools}")
print("All 12 machines registered.")


# ---------------------------------------------------------------------------
# 2. Save client orders
# ---------------------------------------------------------------------------

print("\n=== 2. save_to_db — insert client orders ===")

client_orders = [
    # --- Acme Corp ---
    FakeClientOrder(
        name="Acme Corp", NIF=123456789, OrderID=1001,
        orders=[
            FakeOrderLine("RWW",  10, DDate=3, penalty=50,   priority=1),
            FakeOrderLine("SWM",   5, DDate=5, penalty=20,   priority=None),
            FakeOrderLine("RMM",   4, DDate=7, penalty=15,   priority=None),
        ]
    ),
    FakeClientOrder(
        name="Acme Corp", NIF=123456789, OrderID=1002,
        orders=[
            FakeOrderLine("SWW",   6, DDate=1, penalty=200,  priority=1),
            FakeOrderLine("RWM",   8, DDate=4, penalty=40,   priority=2),
        ]
    ),
    FakeClientOrder(
        name="Acme Corp", NIF=123456789, OrderID=1003,
        orders=[
            FakeOrderLine("SMM",  12, DDate=6, penalty=25,   priority=None),
        ]
    ),
    # --- Globex Ltd ---
    FakeClientOrder(
        name="Globex Ltd", NIF=987654321, OrderID=2001,
        orders=[
            FakeOrderLine("RMM",   8, DDate=2, penalty=100,  priority=1),
            FakeOrderLine("SMM",   3, DDate=4, penalty=30,   priority=2),
            FakeOrderLine("RWM",  15, DDate=6, penalty=10,   priority=None),
        ]
    ),
    FakeClientOrder(
        name="Globex Ltd", NIF=987654321, OrderID=2002,
        orders=[
            FakeOrderLine("SWW",  20, DDate=3, penalty=75,   priority=1),
            FakeOrderLine("RWW",   7, DDate=5, penalty=35,   priority=1),
        ]
    ),
    FakeClientOrder(
        name="Globex Ltd", NIF=987654321, OrderID=2003,
        orders=[
            FakeOrderLine("SWM",   9, DDate=2, penalty=120,  priority=1),
            FakeOrderLine("RMM",   5, DDate=8, penalty=20,   priority=None),
            FakeOrderLine("SMM",   6, DDate=8, penalty=20,   priority=None),
        ]
    ),
    # --- Initech Industries ---
    FakeClientOrder(
        name="Initech Industries", NIF=555000111, OrderID=3001,
        orders=[
            FakeOrderLine("RWW",  30, DDate=5, penalty=300,  priority=1),
            FakeOrderLine("SWW",  30, DDate=5, penalty=300,  priority=1),
        ]
    ),
    FakeClientOrder(
        name="Initech Industries", NIF=555000111, OrderID=3002,
        orders=[
            FakeOrderLine("RWM",  10, DDate=3, penalty=80,   priority=2),
            FakeOrderLine("SWM",  10, DDate=3, penalty=80,   priority=2),
            FakeOrderLine("RMM",  10, DDate=3, penalty=80,   priority=2),
            FakeOrderLine("SMM",  10, DDate=3, penalty=80,   priority=2),
        ]
    ),
    # --- Umbrella Manufacturing ---
    FakeClientOrder(
        name="Umbrella Manufacturing", NIF=666777888, OrderID=4001,
        orders=[
            FakeOrderLine("SMM",   2, DDate=1, penalty=500,  priority=1),
            FakeOrderLine("RMM",   2, DDate=1, penalty=500,  priority=1),
        ]
    ),
    FakeClientOrder(
        name="Umbrella Manufacturing", NIF=666777888, OrderID=4002,
        orders=[
            FakeOrderLine("RWW",  18, DDate=7, penalty=15,   priority=None),
            FakeOrderLine("SWW",  18, DDate=7, penalty=15,   priority=None),
            FakeOrderLine("RWM",   6, DDate=9, penalty=5,    priority=None),
        ]
    ),
    FakeClientOrder(
        name="Umbrella Manufacturing", NIF=666777888, OrderID=4003,
        orders=[
            FakeOrderLine("SWM",   5, DDate=2, penalty=90,   priority=2),
            FakeOrderLine("RWM",   5, DDate=2, penalty=90,   priority=2),
        ]
    ),
    # --- Soylent Corp ---
    FakeClientOrder(
        name="Soylent Corp", NIF=112233445, OrderID=5001,
        orders=[
            FakeOrderLine("RWW",   4, DDate=4, penalty=10,   priority=None),
            FakeOrderLine("SWM",   4, DDate=4, penalty=10,   priority=None),
        ]
    ),
    FakeClientOrder(
        name="Soylent Corp", NIF=112233445, OrderID=5002,
        orders=[
            FakeOrderLine("RMM",   3, DDate=6, penalty=8,    priority=None),
            FakeOrderLine("SMM",   3, DDate=6, penalty=8,    priority=None),
            FakeOrderLine("RWM",   3, DDate=6, penalty=8,    priority=None),
            FakeOrderLine("SWW",   3, DDate=6, penalty=8,    priority=None),
        ]
    ),
    FakeClientOrder(
        name="Soylent Corp", NIF=112233445, OrderID=5003,
        orders=[
            FakeOrderLine("RWW",   6, DDate=10, penalty=5,   priority=None),
            FakeOrderLine("SWW",   6, DDate=10, penalty=5,   priority=None),
        ]
    ),
]

for co in client_orders:
    save_to_db(co)


# ---------------------------------------------------------------------------
# 3. Read pending orders
# ---------------------------------------------------------------------------

print("\n=== 3. get_pending_orders ===")
pending = get_pending_orders()
for o in pending:
    print(f"  order_id={o['order_id']:>3}  type={o['type']:<6}  qty={o['quantity']:>3}"
          f"  DDate={o['DDate']}  penalty={o['penalty']:>4}  priority={o['priority']}")

order_ids = [o['order_id'] for o in pending]
first_oid  = order_ids[0] if order_ids else None
def oid(idx):
    return order_ids[idx] if idx < len(order_ids) else first_oid


# ---------------------------------------------------------------------------
# 4. Machine tool assignments & mode switches
# ---------------------------------------------------------------------------

print("\n=== 4. update_machine_state ===")

tool_assignments = [
    ("M1a",  "T1",  "automatic"),
    ("M1b",  "T2",  "automatic"),
    ("M1c",  "T8",  "automatic"),
    ("M2a",  "T3",  "automatic"),
    ("M2b",  "T1",  "automatic"),
    ("M2c",  "T9",  "automatic"),
    ("M3a",  "T4",  "automatic"),
    ("M3b",  "T5",  "automatic"),
    ("M3c",  "T8",  "automatic"),
    ("M4a",  "T6",  "automatic"),
    ("M4b",  "T4",  "automatic"),
    ("M4c",  "T10", "manual"),
]
for machine, tool, mode in tool_assignments:
    update_machine_state(machine, current_tool=tool, mode=mode)

# Two machines go into maintenance
update_machine_state("M1c", mode="maintenance")
update_machine_state("M2c", mode="maintenance")
print("All machines configured — M1c + M2c in maintenance.")


# ---------------------------------------------------------------------------
# 5. Machining operations — 3 simulated days
#    Each tuple: (machine, tool, piece_type, duration_s, success, order_index)
#    tool_name is now passed into log_production_start so production_log
#    stores which tool was used → enables Grafana time-series per tool
# ---------------------------------------------------------------------------

print("\n=== 5. Production log + tool usage (3 simulated days) ===")

all_ops = [
    # --- Day 1: Wood processing C1/C2 ---
    ("M1a", "T1",  "RtopW",  30, True,  0),
    ("M1a", "T1",  "RtopW",  30, True,  0),
    ("M1a", "T1",  "RtopW",  30, True,  0),
    ("M1b", "T2",  "StopW",  20, True,  1),
    ("M1b", "T2",  "StopW",  20, True,  1),
    ("M2a", "T3",  "LegW",   10, True,  2),
    ("M2a", "T3",  "LegW",   10, True,  2),
    ("M2a", "T3",  "LegW",   10, True,  2),
    ("M2a", "T3",  "LegW",   10, True,  2),
    ("M2b", "T1",  "RtopW",  30, True,  0),
    ("M2b", "T1",  "RtopW",  30, False, 0),   # destroyed
    ("M2b", "T1",  "RtopW",  30, True,  0),
    # --- Day 1: Metal processing C3/C4 ---
    ("M3a", "T4",  "RtopM",  35, True,  3),
    ("M3a", "T4",  "RtopM",  35, True,  3),
    ("M3b", "T5",  "LegM",   30, True,  4),
    ("M3b", "T5",  "LegM",   30, True,  4),
    ("M3b", "T5",  "LegM",   30, True,  4),
    ("M4a", "T6",  "StopM",  25, True,  5),
    ("M4b", "T4",  "RtopM",  35, True,  3),
    ("M4b", "T4",  "RtopM",  35, False, 3),   # destroyed
    # --- Day 1: Assembly ---
    ("M1c", "T8",  "RWW",    10, True,  0),
    ("M3c", "T8",  "RMM",    10, True,  3),
    ("M3c", "T9",  "RWM",    10, True,  6),
    ("M4c", "T10", "LegW",   30, True,  2),

    # --- Day 2: heavier throughput ---
    ("M1a", "T1",  "RtopW",  30, True,  0),
    ("M1a", "T1",  "RtopW",  30, True,  0),
    ("M1a", "T2",  "StopW",  20, True,  1),   # M1a tool change T1→T2
    ("M1b", "T2",  "StopW",  20, True,  1),
    ("M1b", "T2",  "StopW",  20, True,  1),
    ("M1b", "T3",  "LegW",   10, True,  2),   # M1b tool change T2→T3
    ("M2a", "T3",  "LegW",   10, True,  2),
    ("M2a", "T3",  "LegW",   10, True,  2),
    ("M2a", "T1",  "RtopW",  30, True,  0),   # M2a tool change T3→T1
    ("M2b", "T1",  "RtopW",  30, True,  0),
    ("M2b", "T3",  "LegW",   10, True,  2),   # M2b tool change T1→T3
    ("M2b", "T3",  "LegW",   10, True,  2),
    ("M3a", "T4",  "RtopM",  35, True,  3),
    ("M3a", "T5",  "LegM",   30, True,  4),   # M3a tool change T4→T5
    ("M3a", "T5",  "LegM",   30, True,  4),
    ("M3b", "T6",  "StopM",  25, True,  5),   # M3b tool change T5→T6
    ("M3b", "T5",  "LegM",   30, True,  4),
    ("M4a", "T4",  "RtopM",  35, True,  3),   # M4a tool change T6→T4
    ("M4a", "T6",  "StopM",  25, True,  5),
    ("M4b", "T5",  "LegM",   30, True,  4),   # M4b tool change T4→T5
    ("M1c", "T8",  "RWW",    10, True,  0),
    ("M1c", "T8",  "SWW",    10, True,  1),
    ("M3c", "T8",  "RMM",    10, True,  3),
    ("M3c", "T8",  "SMM",    10, True,  7),
    ("M3c", "T9",  "SWM",    10, True,  8),   # M3c tool change T8→T9
    ("M4c", "T9",  "RWM",    10, True,  6),   # M4c tool change T10→T9
    ("M4c", "T10", "LegW",   30, True,  2),

    # --- Day 3: steady state + more failures ---
    ("M1a", "T1",  "RtopW",  30, True,  0),
    ("M1a", "T1",  "RtopW",  30, True,  0),
    ("M1a", "T1",  "RtopW",  30, True,  0),
    ("M1b", "T2",  "StopW",  20, True,  1),
    ("M1b", "T2",  "StopW",  20, False, 1),   # destroyed
    ("M2a", "T3",  "LegW",   10, True,  2),
    ("M2a", "T3",  "LegW",   10, True,  2),
    ("M2b", "T1",  "RtopW",  30, True,  0),
    ("M2b", "T3",  "LegW",   10, True,  2),
    ("M3a", "T4",  "RtopM",  35, True,  3),
    ("M3a", "T4",  "RtopM",  35, True,  3),
    ("M3b", "T5",  "LegM",   30, True,  4),
    ("M3b", "T5",  "LegM",   30, True,  4),
    ("M4a", "T6",  "StopM",  25, True,  5),
    ("M4b", "T4",  "RtopM",  35, True,  3),
    ("M1c", "T8",  "RWW",    10, True,  0),
    ("M1c", "T8",  "RWW",    10, True,  0),
    ("M1c", "T8",  "SWW",    10, True,  1),
    ("M3c", "T8",  "RMM",    10, True,  3),
    ("M3c", "T9",  "RWM",    10, True,  6),
    ("M4c", "T9",  "SWM",    10, True,  8),
    ("M4c", "T10", "LegW",   30, True,  2),
]

for machine, tool, piece, duration, success, oidx in all_ops:
    # tool_name is now stored in production_log → enables time-series per tool
    log_id = log_production_start(oid(oidx), machine, piece, tool_name=tool)
    time.sleep(0.01)
    log_production_end(log_id, success=success)
    if success:
        record_tool_usage(machine, tool, duration_s=duration, pieces=1)
    marker = "OK    " if success else "FAILED"
    print(f"  [{marker}] {machine}/{tool:<4} → {piece:<8} ({duration:>2}s)  log_id={log_id}")


# ---------------------------------------------------------------------------
# 6. Daily machine stats snapshots
# ---------------------------------------------------------------------------

print("\n=== 6. snapshot_machine_stats (3 days) ===")

daily_snapshots = [
    # (day, machine, op_time_s, occ_pct, tool_changes, pieces)
    (1, "M1a",  90, 56.2, 0,  3),
    (1, "M1b",  40, 25.0, 0,  2),
    (1, "M2a",  40, 25.0, 0,  4),
    (1, "M2b",  60, 37.5, 0,  2),
    (1, "M3a",  70, 43.7, 0,  2),
    (1, "M3b",  90, 56.2, 0,  3),
    (1, "M4a",  25, 15.6, 0,  1),
    (1, "M4b",  35, 21.8, 1,  1),
    (1, "M1c",  30, 18.7, 0,  2),
    (1, "M3c",  30, 18.7, 1,  3),
    (1, "M4c",  30, 18.7, 0,  1),
    (2, "M1a", 110, 68.7, 1,  5),
    (2, "M1b",  80, 50.0, 2,  5),
    (2, "M2a",  80, 50.0, 2,  5),
    (2, "M2b",  70, 43.7, 2,  5),
    (2, "M3a", 130, 81.2, 2,  5),
    (2, "M3b",  85, 53.1, 2,  5),
    (2, "M4a",  60, 37.5, 2,  2),
    (2, "M4b",  30, 18.7, 2,  1),
    (2, "M1c",  20, 12.5, 0,  2),
    (2, "M3c",  40, 25.0, 2,  4),
    (2, "M4c",  40, 25.0, 2,  2),
    (3, "M1a",  90, 56.2, 0,  3),
    (3, "M1b",  30, 18.7, 0,  1),
    (3, "M2a",  20, 12.5, 0,  2),
    (3, "M2b",  40, 25.0, 0,  2),
    (3, "M3a",  70, 43.7, 0,  2),
    (3, "M3b",  60, 37.5, 0,  2),
    (3, "M4a",  25, 15.6, 0,  1),
    (3, "M4b",  35, 21.8, 0,  1),
    (3, "M1c",  30, 18.7, 0,  3),
    (3, "M3c",  20, 12.5, 0,  2),
    (3, "M4c",  40, 25.0, 0,  2),
]

for day, machine, op_time, occ, tc, pieces in daily_snapshots:
    snapshot_machine_stats(machine, op_time, occ, tc, pieces)
    print(f"  Day {day}  {machine}  op={op_time:>3}s  occ={occ:>5.1f}%  tc={tc}  pieces={pieces}")


# ---------------------------------------------------------------------------
# 7. Unload events across all 5 docks
# ---------------------------------------------------------------------------

print("\n=== 7. record_unload ===")

unloads = [
    (1, "RWW",  3), (1, "RWW",  4), (1, "RWW",  2),  # dock 1 RWW total=9
    (1, "SWW",  5), (1, "SWW",  3),                   # dock 1 SWW total=8
    (2, "RWM",  6), (2, "RWM",  2),                   # dock 2 RWM total=8
    (2, "SWM",  4), (2, "SWM",  4),                   # dock 2 SWM total=8
    (3, "RMM",  5), (3, "RMM",  3), (3, "RMM",  1),  # dock 3 RMM total=9
    (3, "SMM",  6),                                    # dock 3 SMM total=6
    (4, "SWW",  6), (4, "SWW",  6),                   # dock 4 SWW total=12
    (4, "RWW",  3),                                    # dock 4 RWW total=3
    (5, "RWM",  4), (5, "RWM",  2),                   # dock 5 RWM total=6
    (5, "SWM",  5), (5, "SWM",  1),                   # dock 5 SWM total=6
    (5, "SMM",  3),                                    # dock 5 SMM total=3
]

for dock, piece, count in unloads:
    record_unload(dock, piece, count)
    print(f"  Dock {dock}: +{count:>2}x {piece}")


# ---------------------------------------------------------------------------
# 8. Update order statuses
# ---------------------------------------------------------------------------

print("\n=== 8. update_order_status ===")
for i, o in enumerate(pending):
    if i < 3:
        update_order_status(o['order_id'], 'IN_PROGRESS')
        print(f"  order_id={o['order_id']:>3} → IN_PROGRESS")
    elif i < 5:
        update_order_status(o['order_id'], 'COMPLETED')
        print(f"  order_id={o['order_id']:>3} → COMPLETED")


# ---------------------------------------------------------------------------
# 9. Read-back summaries
# ---------------------------------------------------------------------------

print("\n=== 9. get_tool_usage_summary ===")
for row in get_tool_usage_summary():
    print(f"  {row['machine']:<4}/{row['tool_name']:<5}  "
          f"total={row['total_time_s']:>7.1f}s  pieces={row['pieces_processed']:>3}")

print("\n=== 10. get_unload_summary ===")
for row in get_unload_summary():
    print(f"  dock={row['dock_id']}  type={row['piece_type']:<6}  count={row['count']:>3}")

print("\n=== All done ===")