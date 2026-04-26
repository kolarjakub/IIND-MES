
from order_reciver import OrderReceiver


# Ok so now you have to implement the DB handler that will receive the orders from the order receiver and save them to a database.
# You can pass function to a callback in the order receiver that will be called every time an order is received. 
# This way you can implement the DB handler logic in that function and keep the order receiver code clean and separated from the DB logic.

def save_to_db(order):
    # his database logic here
    print(f"Saving order {order.OrderID} to DB...")
    print(f"Order details: {order}")
    print(f"Total orders received so far: {receiver.get_orders_received()}")

receiver = OrderReceiver(on_order_received=save_to_db)

receiver.start_server()
receiver.receive_orders()