import psycopg2
import logging
from datetime import datetime

# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def db_connect():
    try:
        connection = psycopg2.connect(
            host="db.fe.up.pt", database="meec00909",
            user="meec00909", password="pabloEscobar"
        )
        cursor = connection.cursor()
        return cursor, connection
    except Exception as e:
        print(f"Connection to DB was NOT successful: {e}")
        return None, None


def db_disconnect(connection):
    if not connection:
        return
    try:
        connection.commit()
    except Exception:
        try:
            connection.rollback()
        except Exception:
            pass
    finally:
        try:
            connection.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Schema init  (call once at startup — drops & recreates mes schema)
# ---------------------------------------------------------------------------

def db_init():
    cursor, connection = db_connect()
    if cursor is None:
        return
    try:
        statements = [
            "DROP SCHEMA IF EXISTS mes CASCADE",
            "CREATE SCHEMA mes",

            # --- ERP / order side ---
            """CREATE TABLE mes.clients (
                client_id   SERIAL PRIMARY KEY,
                name        VARCHAR(255) NOT NULL,
                nif         BIGINT,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (name, nif)
            )""",
            """CREATE TABLE mes.client_orders (
                client_order_id   SERIAL PRIMARY KEY,
                external_order_id INT,
                client_id         INT REFERENCES mes.clients(client_id) ON DELETE CASCADE,
                created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (external_order_id, client_id)
            )""",
            """CREATE TABLE mes.orders (
                order_id        SERIAL PRIMARY KEY,
                client_order_id INT REFERENCES mes.client_orders(client_order_id) ON DELETE CASCADE,
                type            VARCHAR(255) NOT NULL,
                quantity        INT  NOT NULL,
                DDate           INT  NOT NULL,
                penalty         INT  NOT NULL,
                priority        INT  DEFAULT NULL,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status          TEXT NOT NULL CHECK (status IN ('PENDING','IN_PROGRESS','COMPLETED'))
            )""",

            # --- Machine / shop-floor side ---
            """CREATE TABLE mes.machines (
                machine_id      SERIAL PRIMARY KEY,
                name            VARCHAR(16)  NOT NULL UNIQUE,
                cell            VARCHAR(8)   NOT NULL,
                current_tool    VARCHAR(8),
                available_tools VARCHAR(8)[] NOT NULL DEFAULT '{}',
                mode            VARCHAR(16)  NOT NULL DEFAULT 'automatic'
                                    CHECK (mode IN ('automatic','manual','maintenance')),
                updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE mes.tools (
                tool_id    SERIAL PRIMARY KEY,
                machine_id INT REFERENCES mes.machines(machine_id) ON DELETE CASCADE,
                tool_name  VARCHAR(8) NOT NULL,
                is_mounted BOOL NOT NULL DEFAULT FALSE,
                UNIQUE (machine_id, tool_name)
            )""",
            """CREATE TABLE mes.tool_usage (
                usage_id         SERIAL PRIMARY KEY,
                machine_id       INT REFERENCES mes.machines(machine_id) ON DELETE CASCADE,
                tool_name        VARCHAR(8) NOT NULL,
                total_time_s     FLOAT NOT NULL DEFAULT 0,
                pieces_processed INT   NOT NULL DEFAULT 0,
                updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (machine_id, tool_name)
            )""",
            """CREATE TABLE mes.machine_stats (
                stat_id         SERIAL PRIMARY KEY,
                machine_id      INT REFERENCES mes.machines(machine_id) ON DELETE CASCADE,
                recorded_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                total_op_time_s FLOAT NOT NULL DEFAULT 0,
                occupation_pct  FLOAT NOT NULL DEFAULT 0,
                tool_changes    INT   NOT NULL DEFAULT 0,
                pieces_total    INT   NOT NULL DEFAULT 0
            )""",
            """CREATE TABLE mes.unload_stats (
                stat_id    SERIAL PRIMARY KEY,
                dock_id    INT  NOT NULL,
                piece_type VARCHAR(16) NOT NULL,
                count      INT  NOT NULL DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (dock_id, piece_type)
            )""",
            """CREATE TABLE mes.production_log (
                log_id      SERIAL PRIMARY KEY,
                order_id    INT REFERENCES mes.orders(order_id) ON DELETE SET NULL,
                machine_id  INT REFERENCES mes.machines(machine_id) ON DELETE SET NULL,
                piece_type  VARCHAR(16) NOT NULL,
                tool_name   VARCHAR(8),
                status      TEXT NOT NULL CHECK (status IN ('started','completed','failed')),
                started_at  TIMESTAMP,
                finished_at TIMESTAMP
            )""",
        ]

        for stmt in statements:
            cursor.execute(stmt)

        connection.commit()
        print("Schema mes created successfully.")
    except Exception as e:
        print(f"Error creating schema: {e}")
        try:
            connection.rollback()
        except Exception:
            pass
    finally:
        db_disconnect(connection)


# ---------------------------------------------------------------------------
# ERP / order queries  (original logic, kept intact)
# ---------------------------------------------------------------------------

_insert_client_q = """
    INSERT INTO mes.clients (name, nif)
    VALUES (%s, %s)
    ON CONFLICT (name, nif) DO UPDATE SET name = EXCLUDED.name
    RETURNING client_id;
"""

_insert_client_order_q = """
    INSERT INTO mes.client_orders (external_order_id, client_id)
    VALUES (%s, %s)
    ON CONFLICT (external_order_id, client_id) DO NOTHING
    RETURNING client_order_id;
"""

_insert_order_q = """
    INSERT INTO mes.orders (client_order_id, type, quantity, DDate, penalty, priority, status)
    VALUES (%s, %s, %s, %s, %s, %s, 'PENDING');
"""


def save_to_db(client_order):
    """Callback passed to OrderReceiver — persists a full ClientOrder."""
    cursor, connection = db_connect()
    if cursor is None:
        return
    try:
        # upsert client
        cursor.execute(_insert_client_q, (client_order.name, client_order.NIF))
        row = cursor.fetchone()
        if row:
            client_id = row[0]
        else:
            cursor.execute(
                "SELECT client_id FROM mes.clients WHERE name=%s AND nif=%s",
                (client_order.name, client_order.NIF)
            )
            client_id = cursor.fetchone()[0]

        # upsert client_order
        cursor.execute(_insert_client_order_q, (client_order.OrderID, client_id))
        row = cursor.fetchone()
        if row:
            client_order_id = row[0]
        else:
            cursor.execute(
                "SELECT client_order_id FROM mes.client_orders "
                "WHERE external_order_id=%s AND client_id=%s",
                (client_order.OrderID, client_id)
            )
            client_order_id = cursor.fetchone()[0]

        # insert order lines
        for item in getattr(client_order, 'orders', []):
            cursor.execute(_insert_order_q,
                           (client_order_id, item.type, item.quantity,
                            item.DDate, item.Penalty,
                            getattr(item, 'priority', None)))

        connection.commit()
        print(f"Saved client order {client_order.OrderID} to DB.")
    except Exception as e:
        print(f"Error saving order: {e}")
        try:
            connection.rollback()
        except Exception:
            pass
    finally:
        db_disconnect(connection)


# ---------------------------------------------------------------------------
# Machine / tool setup queries
# ---------------------------------------------------------------------------

def register_machine(name: str, cell: str, tools: list[str]):
    """Insert a machine and its tool warehouse. Safe to call at startup.

    Args:
        name:  machine identifier, e.g. 'M1a'
        cell:  cell identifier, e.g. 'C1'
        tools: list of tool names available in this machine's warehouse,
               e.g. ['T1', 'T2', 'T3']
    """
    cursor, connection = db_connect()
    if cursor is None:
        return
    try:
        cursor.execute("""
            INSERT INTO mes.machines (name, cell, available_tools)
            VALUES (%s, %s, %s)
            ON CONFLICT (name) DO UPDATE SET available_tools = EXCLUDED.available_tools
            RETURNING machine_id;
        """, (name, cell, tools))
        row = cursor.fetchone()
        if row:
            machine_id = row[0]
        else:
            cursor.execute("SELECT machine_id FROM mes.machines WHERE name=%s", (name,))
            machine_id = cursor.fetchone()[0]

        for tool in tools:
            cursor.execute("""
                INSERT INTO mes.tools (machine_id, tool_name, is_mounted)
                VALUES (%s, %s, FALSE)
                ON CONFLICT (machine_id, tool_name) DO NOTHING;
            """, (machine_id, tool))

        connection.commit()
    except Exception as e:
        print(f"Error registering machine {name}: {e}")
        try:
            connection.rollback()
        except Exception:
            pass
    finally:
        db_disconnect(connection)


def update_machine_state(name: str, current_tool: str = None, mode: str = None):
    """Update the live state of a machine (tool change or mode change).

    Args:
        name:         machine name, e.g. 'M1a'
        current_tool: tool currently mounted, e.g. 'T2' (None = no change)
        mode:         one of 'automatic', 'manual', 'maintenance' (None = no change)
    """
    cursor, connection = db_connect()
    if cursor is None:
        return
    try:
        if current_tool is not None:
            cursor.execute("""
                UPDATE mes.machines
                SET current_tool = %s, updated_at = NOW()
                WHERE name = %s;
            """, (current_tool, name))
            # flip is_mounted in tools table
            cursor.execute("""
                UPDATE mes.tools SET is_mounted = (tool_name = %s)
                WHERE machine_id = (SELECT machine_id FROM mes.machines WHERE name = %s);
            """, (current_tool, name))

        if mode is not None:
            cursor.execute("""
                UPDATE mes.machines
                SET mode = %s, updated_at = NOW()
                WHERE name = %s;
            """, (mode, name))

        connection.commit()
    except Exception as e:
        print(f"Error updating machine {name}: {e}")
        try:
            connection.rollback()
        except Exception:
            pass
    finally:
        db_disconnect(connection)


# ---------------------------------------------------------------------------
# Tool usage queries  (called after every machining operation)
# ---------------------------------------------------------------------------

def record_tool_usage(machine_name: str, tool_name: str,
                      duration_s: float, pieces: int = 1):
    """Upsert cumulative tool-usage counters for one machining operation.

    Args:
        machine_name: e.g. 'M1a'
        tool_name:    e.g. 'T1'
        duration_s:   how long the tool ran (seconds)
        pieces:       number of pieces produced in this operation (usually 1)
    """
    cursor, connection = db_connect()
    if cursor is None:
        return
    try:
        cursor.execute("""
            INSERT INTO mes.tool_usage (machine_id, tool_name, total_time_s, pieces_processed)
            SELECT machine_id, %s, %s, %s
            FROM mes.machines WHERE name = %s
            ON CONFLICT (machine_id, tool_name) DO UPDATE
                SET total_time_s    = mes.tool_usage.total_time_s    + EXCLUDED.total_time_s,
                    pieces_processed = mes.tool_usage.pieces_processed + EXCLUDED.pieces_processed,
                    updated_at       = NOW();
        """, (tool_name, duration_s, pieces, machine_name))
        connection.commit()
    except Exception as e:
        print(f"Error recording tool usage for {machine_name}/{tool_name}: {e}")
        try:
            connection.rollback()
        except Exception:
            pass
    finally:
        db_disconnect(connection)


# ---------------------------------------------------------------------------
# Machine stats snapshot  (call once per simulated day = 60 s)
# ---------------------------------------------------------------------------

def snapshot_machine_stats(machine_name: str, total_op_time_s: float,
                            occupation_pct: float, tool_changes: int,
                            pieces_total: int):
    """Append a stats snapshot for one machine.  Grafana reads this as a time series.

    Args:
        machine_name:    e.g. 'M1a'
        total_op_time_s: cumulative operating time in seconds
        occupation_pct:  0..100 percentage
        tool_changes:    cumulative number of tool changes
        pieces_total:    cumulative pieces processed
    """
    cursor, connection = db_connect()
    if cursor is None:
        return
    try:
        cursor.execute("""
            INSERT INTO mes.machine_stats
                (machine_id, total_op_time_s, occupation_pct, tool_changes, pieces_total)
            SELECT machine_id, %s, %s, %s, %s
            FROM mes.machines WHERE name = %s;
        """, (total_op_time_s, occupation_pct, tool_changes, pieces_total, machine_name))
        connection.commit()
    except Exception as e:
        print(f"Error snapshotting stats for {machine_name}: {e}")
        try:
            connection.rollback()
        except Exception:
            pass
    finally:
        db_disconnect(connection)


# ---------------------------------------------------------------------------
# Unload dock queries
# ---------------------------------------------------------------------------

def record_unload(dock_id: int, piece_type: str, count: int = 1):
    """Increment the unload counter for a given dock and piece type.

    Args:
        dock_id:    1..5
        piece_type: e.g. 'RWW'
        count:      number of pieces unloaded (default 1)
    """
    cursor, connection = db_connect()
    if cursor is None:
        return
    try:
        cursor.execute("""
            INSERT INTO mes.unload_stats (dock_id, piece_type, count)
            VALUES (%s, %s, %s)
            ON CONFLICT (dock_id, piece_type) DO UPDATE
                SET count      = mes.unload_stats.count + EXCLUDED.count,
                    updated_at = NOW();
        """, (dock_id, piece_type, count))
        connection.commit()
    except Exception as e:
        print(f"Error recording unload dock={dock_id} type={piece_type}: {e}")
        try:
            connection.rollback()
        except Exception:
            pass
    finally:
        db_disconnect(connection)


# ---------------------------------------------------------------------------
# Production log queries
# ---------------------------------------------------------------------------

def log_production_start(order_id: int, machine_name: str, piece_type: str,
                          tool_name: str = None) -> int | None:
    """Log the start of processing one piece. Returns the new log_id.

    Args:
        order_id:     mes.orders.order_id this piece belongs to
        machine_name: e.g. 'M1a'
        piece_type:   e.g. 'RtopW'
        tool_name:    tool currently mounted, e.g. 'T1'
    """
    cursor, connection = db_connect()
    if cursor is None:
        return None
    try:
        cursor.execute("""
            INSERT INTO mes.production_log
                (order_id, machine_id, piece_type, tool_name, status, started_at)
            SELECT %s, machine_id, %s, %s, 'started', NOW()
            FROM mes.machines WHERE name = %s
            RETURNING log_id;
        """, (order_id, piece_type, tool_name, machine_name))
        row = cursor.fetchone()
        connection.commit()
        return row[0] if row else None
    except Exception as e:
        print(f"Error logging production start: {e}")
        try:
            connection.rollback()
        except Exception:
            pass
        return None
    finally:
        db_disconnect(connection)


def log_production_end(log_id: int, success: bool = True):
    """Mark a production log entry as completed or failed.

    Args:
        log_id:  the id returned by log_production_start
        success: True → 'completed', False → 'failed'
    """
    cursor, connection = db_connect()
    if cursor is None:
        return
    try:
        status = 'completed' if success else 'failed'
        cursor.execute("""
            UPDATE mes.production_log
            SET status = %s, finished_at = NOW()
            WHERE log_id = %s;
        """, (status, log_id))
        connection.commit()
    except Exception as e:
        print(f"Error logging production end for log_id={log_id}: {e}")
        try:
            connection.rollback()
        except Exception:
            pass
    finally:
        db_disconnect(connection)


# ---------------------------------------------------------------------------
# Order status helpers  (used by MES scheduler)
# ---------------------------------------------------------------------------

def get_pending_orders():
    """Return all PENDING orders sorted by DDate ascending (earliest deadline first).

    Returns list of dicts with keys:
        order_id, type, quantity, DDate, penalty, created_at
    """
    cursor, connection = db_connect()
    if cursor is None:
        return []
    try:
        cursor.execute("""
            SELECT order_id, type, quantity, "DDate", penalty, priority, created_at
            FROM mes.orders
            WHERE status = 'PENDING'
            ORDER BY priority ASC NULLS LAST, "DDate" ASC, penalty DESC;
        """)
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]
    except Exception as e:
        print(f"Error fetching pending orders: {e}")
        return []
    finally:
        db_disconnect(connection)


def update_order_status(order_id: int, status: str):
    """Set the status of an order.

    Args:
        order_id: mes.orders.order_id
        status:   'PENDING' | 'IN_PROGRESS' | 'COMPLETED'
    """
    cursor, connection = db_connect()
    if cursor is None:
        return
    try:
        cursor.execute("""
            UPDATE mes.orders SET status = %s WHERE order_id = %s;
        """, (status, order_id))
        connection.commit()
    except Exception as e:
        print(f"Error updating order {order_id} status: {e}")
        try:
            connection.rollback()
        except Exception:
            pass
    finally:
        db_disconnect(connection)


# ---------------------------------------------------------------------------
# Grafana read-only helpers  (convenient for debugging / API endpoints)
# ---------------------------------------------------------------------------

def get_tool_usage_summary():
    """Return cumulative tool usage across all machines, ordered by total time.

    Returns list of dicts: machine, tool_name, total_time_s, pieces_processed
    """
    cursor, connection = db_connect()
    if cursor is None:
        return []
    try:
        cursor.execute("""
            SELECT m.name AS machine, tu.tool_name,
                   tu.total_time_s, tu.pieces_processed, tu.updated_at
            FROM mes.tool_usage tu
            JOIN mes.machines m USING (machine_id)
            ORDER BY tu.total_time_s DESC;
        """)
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]
    except Exception as e:
        print(f"Error fetching tool usage: {e}")
        return []
    finally:
        db_disconnect(connection)


def get_unload_summary():
    """Return total unloaded pieces per dock and per type.

    Returns list of dicts: dock_id, piece_type, count
    """
    cursor, connection = db_connect()
    if cursor is None:
        return []
    try:
        cursor.execute("""
            SELECT dock_id, piece_type, count, updated_at
            FROM mes.unload_stats
            ORDER BY dock_id, piece_type;
        """)
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]
    except Exception as e:
        print(f"Error fetching unload summary: {e}")
        return []
    finally:
        db_disconnect(connection)


# ---------------------------------------------------------------------------
# Truncate (dev / testing only)
# ---------------------------------------------------------------------------

def db_truncate():
    cursor, connection = db_connect()
    if cursor is None:
        return
    try:
        cursor.execute("""
            TRUNCATE mes.production_log, mes.unload_stats, mes.machine_stats,
                     mes.tool_usage, mes.tools, mes.machines,
                     mes.orders, mes.client_orders, mes.clients
            CASCADE;
        """)
        connection.commit()
    except Exception as e:
        print(f"Error truncating: {e}")
    finally:
        db_disconnect(connection)


# ---------------------------------------------------------------------------
# Startup  — init schema + register all 12 machines with their tools
# ---------------------------------------------------------------------------

_MACHINE_TOOLS = {
    # Cell C1
    'M1a': ('C1', ['T1', 'T2', 'T3']),
    'M1b': ('C1', ['T1', 'T2', 'T3']),
    'M1c': ('C1', ['T8', 'T9', 'T11']),
    # Cell C2
    'M2a': ('C2', ['T1', 'T2', 'T3']),
    'M2b': ('C2', ['T1', 'T2', 'T3']),
    'M2c': ('C2', ['T8', 'T9', 'T10']),
    # Cell C3
    'M3a': ('C3', ['T4', 'T5', 'T6']),
    'M3b': ('C3', ['T4', 'T5', 'T6']),
    'M3c': ('C3', ['T8', 'T9', 'T11']),
    # Cell C4
    'M4a': ('C4', ['T4', 'T5', 'T6']),
    'M4b': ('C4', ['T4', 'T5', 'T6']),
    'M4c': ('C4', ['T8', 'T9', 'T10']),
}

if __name__ == "__main__":
    db_init()
    for machine_name, (cell, tools) in _MACHINE_TOOLS.items():
        register_machine(machine_name, cell, tools)
    print("All machines registered.")

    from order_receiver import OrderReceiver
    receiver = OrderReceiver(on_order_received=save_to_db)
    receiver.start_server()
    receiver.receive_orders()