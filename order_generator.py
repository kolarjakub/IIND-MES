import random
import socket
import json
from dataclasses import dataclass

from colorama import init, Fore, Style
init(autoreset=True)

VALID_TYPES = ["RWW", "SWW", "RWM", "SWM", "RMM", "SMM"]

DEFAULT_FRIEND_NAMES = ["Mike", "Jakub", "Joao", "Tiago"]
DEFAULT_WORDS = [
    "Banana",
    "Bar",
    "Beer",
    "Piernik",
    "Szczescie",
    "Onomatopeja",
    "Panda",
    "Rocket",
    "Giggle",
    "Meme"
]

@dataclass
class Order:
    type: str
    quantity: int
    DDate: int
    Penalty: int

@dataclass
class ClientOrder:
    name: str
    NIF: int
    OrderID: int
    orders: list[Order]


def generate_random_company_name(friend_names=None, words=None):
    friend_names = friend_names or DEFAULT_FRIEND_NAMES
    words = words or DEFAULT_WORDS

    selected_names = random.sample(friend_names, k=random.randint(1, min(2, len(friend_names))))
    selected_word = random.choice(words)
    suffix = random.choice(["Works", "Factory", "Industries", "Labs", "Dynamics", "Dump", "Brothel", "Empire", "Moms"])

    return f"{'-'.join(selected_names)} {selected_word} {suffix}"

def random_order():
    return Order(
        type=random.choice(VALID_TYPES),
        quantity=random.randint(1, 20),
        DDate=random.randint(5, 30),
        Penalty=random.randint(50, 500)
    )
def generate_random_client_order():
    return ClientOrder(
        name=generate_random_company_name(),
        NIF=random.randint(100000000, 999999999),
        OrderID=random.randint(1, 1000),
        orders=[random_order() for _ in range(random.randint(1, 5))]
    )

def validate_client_order(order):
    if not order.name or not isinstance(order.name, str):
        raise ValueError("Invalid name")

    if not (100000000 <= order.NIF <= 999999999):
        raise ValueError("Invalid NIF")

    if not (1 <= order.OrderID <= 1000):
        raise ValueError("Invalid OrderID")

    for o in order.orders:
        if o.type not in VALID_TYPES:
            raise ValueError(f"Invalid order type: {o.type}")
        if o.quantity <= 0:
            raise ValueError("Quantity must be positive")
        if o.DDate <= 0:
            raise ValueError("DDate must be positive")
        if o.Penalty < 0:
            raise ValueError("Penalty cannot be negative")

    return True, "Order is valid"

def send_client_order(order, host='localhost', port=6666):
    if validate_client_order(order):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.connect((host, port))
                print(f"{Fore.GREEN}Connected to server at {host}:{port}{Style.RESET_ALL}")
                order_json = json.dumps(order.__dict__, default=lambda o: o.__dict__)
                s.sendall(order_json.encode('utf-8'))
            except Exception as e:
                print(f"{Fore.RED}Failed to send order: {e}{Style.RESET_ALL}")
    else:
        print(f"{Fore.RED}Order validation failed{Style.RESET_ALL}")
# Example_

if __name__ == "__main__":
    for _ in range(5):
        order = generate_random_client_order()
        print(f"Generated Order: {order}")
        send_client_order(order)


