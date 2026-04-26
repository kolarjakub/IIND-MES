
from order_receiver import OrderReceiver

def save_to_db(order):
    # his database logic here
    print(f"Saving order {order.OrderID} to DB...")

receiver = OrderReceiver(on_order_received=save_to_db)
receiver.start_server()
receiver.receive_orders()