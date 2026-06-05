
from dataclasses import dataclass

VALID_TYPES = ["RWW", "SWW", "RWM", "SWM", "RMM", "SMM"]

ESTIMATED_TIME = {
    "RWW": 60,
    "SWW": 50,
    "RWM": 100,
    "SWM": 90,
    "RMM": 105,
    "SMM": 95
}

RAW_MATERIALS = {
    "RWW": {"Wood": 3, "Metal": 0},
    "SWW": {"Wood": 3, "Metal": 0},
    "RWM": {"Wood": 1, "Metal": 2},
    "SWM": {"Wood": 1, "Metal": 2},
    "RMM": {"Wood": 0, "Metal": 3},
    "SMM": {"Wood": 0, "Metal": 3}
}



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
@dataclass
class ActiveOrder:
    client_order_id: int
    piece_type: str
    quantity: int
    quantity_done: int = 0
    ddate_days: int = 0
    penalty: int = 0
    status: str = "PENDING"
    priority: float = 0.0
    started_at: float = 0.0
    db_order_id: int = None

    @property
    def quantity_remaining(self):
        return self.quantity - self.quantity_done
    
    @property
    def estimated_time_remaining(self):
        return self.quantity_remaining * ESTIMATED_TIME.get(self.piece_type, 0)
        
    def calculate_priority(self, in_progress_boost: float) -> float:
        t = self.estimated_time_remaining 
        if t == 0:
            return 0.0
        base = self.penalty / t
        boost = in_progress_boost if self.status == "IN_PROGRESS" else 1.0
        self.priority = base * boost
        return self.priority
    
@dataclass
class WarehouseState:
    wood: int
    metal: int

    @property
    def total(self):
        return self.wood + self.metal