
from dataclasses import dataclass

VALID_TYPES = ["RWW", "SWW", "RWM", "SWM", "RMM", "SMM"]

@dataclass
class Order:
    type: str
    quantity: int
    DDate: int
    Penalty: int

    @classmethod
    def from_dict(cls, data):
        return cls(
            type=data['type'],
            quantity=data['quantity'],
            DDate=data['DDate'],
            Penalty=data['Penalty']
        )

@dataclass
class ClientOrder:
    name: str
    NIF: int
    OrderID: int
    orders: list[Order]

    @classmethod
    def from_dict(cls, data):
        return cls(
            name=data['name'],
            NIF=data['NIF'],
            OrderID=data['OrderID'],
            orders=[Order.from_dict(o) for o in data['orders']]
        )