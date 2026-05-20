import threading
from mes import MES
from order_generator import generate_random_client_order

mes = MES()

# Start MES in background thread
t = threading.Thread(target=mes.run, daemon=True)
t.start()

import time
time.sleep(2)  # let MES start up

# ERP adds materials
mes.add_materials(wood=9, metal=0)  # enough for 3x RWW

# ERP sends an order
from order_generator import send_client_order
from orders import ClientOrder, Order
order = ClientOrder(
    name="Test ERP",
    NIF=123456789,
    OrderID=1,
    orders=[Order(type="RWW", quantity=1, DDate=5, Penalty=100)]
)
send_client_order(order)

# Check status
time.sleep(3)
print(mes.get_status())

# Keep alive
input("Press Enter to stop...")