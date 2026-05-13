
import psycopg2
import logging
from order_receiver import OrderReceiver



# Ok so now you have to implement the DB handler that will receive the orders from the order receiver and save them to a database.
# You can pass function to a callback in the order receiver that will be called every time an order is received. 
# This way you can implement the DB handler logic in that function and keep the order receiver code clean and separated from the DB logic.

def db_connect():
    try:
        connection = psycopg2.connect(
            host="db.fe.up.pt", database="meec00909",
            user="meec00909", password="pabloEscobar"
        )
        cursor = connection.cursor()
        #print("Connection to DB was succesful.")
        return cursor, connection
    except Exception as e:
        print(f"Connection to DB was NOT succesful: {e}")
        return None, None


def db_disconnect(connection):
    # Safely commit and close a connection. If connection is None or already closed,
    # swallow exceptions to avoid raising when called from finally blocks.
    if not connection:
        return

    try:
        connection.commit()  # Make sure any changes are saved
    except Exception:
        # If commit fails, try to rollback to leave DB in a consistent state
        try:
            connection.rollback()
        except Exception:
            pass
    finally:
        try:
            connection.close()
        except Exception:
            pass
    #print("Disconnected from DB.")

def db_init():
    cursor, connection = db_connect()

    try:
        # Drop the schema first so db_init() recreates it from scratch
        cursor.execute(
            """
            DROP SCHEMA IF EXISTS mes CASCADE;
            CREATE SCHEMA mes;

            -- Clients (the "objednavatel")
            CREATE TABLE mes.clients (
                client_id SERIAL PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                nif BIGINT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (name, nif)
            );

            -- Client-level orders (groups of order lines coming from a single client)
            CREATE TABLE mes.client_orders (
                client_order_id SERIAL PRIMARY KEY,
                external_order_id INT,
                client_id INT REFERENCES mes.clients(client_id) ON DELETE CASCADE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (external_order_id, client_id)
            );

            -- Individual order lines
            CREATE TABLE mes.orders (
                order_id SERIAL PRIMARY KEY,
                client_order_id INT REFERENCES mes.client_orders(client_order_id) ON DELETE CASCADE,
                type VARCHAR(255) NOT NULL,
                quantity INT NOT NULL,
                DDate INT NOT NULL,
                penalty INT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT NOT NULL CHECK (status IN ('PENDING','IN_PROGRESS','COMPLETED'))
            );
            """
        )
        connection.commit()
        #print("Table 'orders' created successfully or already exists.")
    except Exception as e:
        print(f"Error creating table: {e}")
    finally:
        db_disconnect(connection)


insert_order_query ="""
    INSERT INTO mes.orders (client_order_id, type, quantity, DDate, penalty, status)
    VALUES (%s, %s, %s, %s, %s, %s);
    """

insert_client_query = """
    INSERT INTO mes.clients (name, nif)
    VALUES (%s, %s)
    ON CONFLICT (name, nif) DO UPDATE SET name = EXCLUDED.name
    RETURNING client_id;
"""

insert_client_order_query = """
    INSERT INTO mes.client_orders (external_order_id, client_id)
    VALUES (%s, %s)
    ON CONFLICT (external_order_id, client_id) DO NOTHING
    RETURNING client_order_id;
"""

def save_to_db(client_order):
    """Save a received ClientOrder to the DB.

    The OrderReceiver passes a ClientOrder instance (see `orders.ClientOrder`) which
    contains a list of `Order` items in `client_order.orders`. Insert one DB row per
    inner Order. If a single Order is passed directly, handle that as well.
    """
    # Basic logging
    try:
        order_id = getattr(client_order, 'OrderID', None)
        print(f"Saving client order {order_id} to DB...")
        print(f"Client order details: {client_order}")
    except Exception:
        print("Saving unknown order object to DB...")

    cursor, connection = db_connect()
    if cursor is None:
        print("Cannot save to DB: no connection")
        return

    try:
        # Ensure client exists (upsert)
        client_name = getattr(client_order, 'name', None)
        client_nif = getattr(client_order, 'NIF', None)
        cursor.execute(insert_client_query, (client_name, client_nif))
        res = cursor.fetchone()
        if res:
            client_id = res[0]
        else:
            # If RETURNING didn't return (conflict path), fetch existing id
            cursor.execute("SELECT client_id FROM mes.clients WHERE name = %s AND nif = %s", (client_name, client_nif))
            client_id = cursor.fetchone()[0]

        # Ensure client_order exists
        external_order_id = getattr(client_order, 'OrderID', None)
        cursor.execute(insert_client_order_query, (external_order_id, client_id))
        res = cursor.fetchone()
        if res:
            client_order_id = res[0]
        else:
            # fetch existing
            cursor.execute("SELECT client_order_id FROM mes.client_orders WHERE external_order_id = %s AND client_id = %s", (external_order_id, client_id))
            client_order_id = cursor.fetchone()[0]

        # Insert order lines
        if hasattr(client_order, 'orders') and isinstance(client_order.orders, (list, tuple)):
            for idx, item in enumerate(client_order.orders):
                try:
                    insert_data = (client_order_id, item.type, item.quantity, item.DDate, item.Penalty, 'PENDING')
                    cursor.execute(insert_order_query, insert_data)
                    print(f"  -> saved line {idx} (type={item.type}, qty={item.quantity}) for client_order_id={client_order_id}")
                except Exception as e:
                    print(f"  Error saving line {idx} of client order {order_id}: {e}")
        else:
            print(f"  No 'orders' list found in client order {order_id}, skipping line insertion.")
        connection.commit()
    except Exception as e:
        print(f"Error saving order to DB: {e}")
        try:
            connection.rollback()
        except Exception:
            pass
    finally:
        db_disconnect(connection)

def db_truncate():
    cursor, connection = db_connect()
    try:
        cursor.execute("TRUNCATE TABLE mes.orders CASCADE")
        cursor.execute("TRUNCATE TABLE mes.client_orders CASCADE")
        cursor.execute("TRUNCATE TABLE mes.clients CASCADE")
    except Exception as e:
        print(f"Error deleting entries: {e}")
    finally:
        db_disconnect(connection)


def db_get_active_orders():
    # later implementation
    pass


db_init()

# db_truncate()

receiver = OrderReceiver(on_order_received=save_to_db)

receiver.start_server()
receiver.receive_orders()