from opcua_handler import OPCUAHandler
import db_handler as dbh


#TODO: 1 pyQt window with a button to start the MES, and a text area to show logs

class MES:
    def __init__(self):
        pass

    def active_orders(self):
        pass
    def pending_orders(self):
        pass
    def completed_orders(self):
        pass
    def send_order_to_opcua(self, order):
        pass
    def complete_order(self, order_id):
        pass
    def calculate_priority(self, order):
        pass
    def update_priorities(self):
        pass
    def threding(self):
        pass
    def gui(self):
        pass
    def run_opcua_handler(self):
        pass
    def calculate_money(self, order):
        pass
    def calculate_scheduling(self, order):
        pass
    def order_more_materials(self, order):
        pass
    def check_warehouse(self, order):
        pass

