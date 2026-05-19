from opcua_handler import OPCUAHandler
import db_handler as dbh
import order_receiver as orc
import threading


#TODO: 1 pyQt window with a button to start the MES, and a text area to show logs



class MES:
    def __init__(self):

        self._opcua_handler = OPCUAHandler()
        self._order_receiver = orc.OrderReceiver(on_order_received=[self._add_order_to_active_list, dbh.save_to_db])

        self._warehouse_W1 = list()
        self._warehouse_W2 = list()

        self._active_orders_list = list()

    def run(self):
        # Start the order receiver in a separate thread
        order_thread = threading.Thread(target=self._receive_orders, args=(self._order_receiver,))
        order_thread.start()

        # Start the scheduler loop in a separate thread
        scheduler_thread = threading.Thread(target=self._scheduler_loop)
        scheduler_thread.start()

        # Start the status loop in a separate thread
        status_thread = threading.Thread(target=self._status_loop)
        status_thread.start()

    def _receive_orders(self, order):
        self._order_receiver.start_server()
        self._order_receiver.receive_orders()

    def _scheduler_loop(self):
        pass

    def _status_loop(self):
        pass

    def _add_order_to_active_list(self, order):
        self._active_orders_list.append(order)
    

# erp -> disect_order - > check_warehouse -> calculate_scheduling -> calculate_priority -> send_order_to_opcua
# -> wait for completion - > chanage order status to completed -> calculate_money