
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
    connection.commit()  # Make sure any changes are saved
    connection.close()
    #print("Disconnected from DB.")

def db_init():
    cursor, connection = db_connect()

    if cursor is None or connection is None:
        print("Database connection failed. Cannot initialize DB.")
        return

    try:
        cursor.execute("""
            CREATE SCHEMA IF NOT EXISTS mes;

            CREATE TABLE IF NOT EXISTS mes.orders (
                order_id SERIAL PRIMARY KEY,
                type VARCHAR(255) NOT NULL,
                quantity INT NOT NULL,
                DDate INT NOT NULL,
                penalty INT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)
            """
        )
        connection.commit()
        #print("Table 'orders' created successfully or already exists.")
    except Exception as e:
        print(f"Error creating table: {e}")
    finally:
        db_disconnect(connection)


def save_to_db(order):
    # his database logic here
    print(f"Saving order {order.OrderID} to DB...")
    print(f"Order details: {order}")
    print(f"Total orders received so far: {receiver.get_orders_received()}")

    # A02_C meec00909 EuwWxGtQUosB


db_init()

receiver = OrderReceiver(on_order_received=save_to_db)

receiver.start_server()
receiver.receive_orders()