import random
import socket
import json
import argparse
from orders import ClientOrder, Order, VALID_TYPES

from colorama import init, Fore, Style
init(autoreset=True)



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
# order_generator.py builds and sends JSON-encoded ClientOrder objects to the MES receiver over a TCP socket on port 6666.
# Orders can be generated randomly (random company name, valid product type, quantity, delivery date, penalty) or entered manually via CLI prompts.
# A third mode generates one order for every valid piece type with quantity fixed to 1.
# Each order is validated before sending — checking NIF format, valid product types, positive quantities, and non-negative penalties.
# Run with -r for a single random order, -m for manual input, -e for every-piece mode, or with no flags for an interactive loop.
# Import generate_random_client_order() and send_client_order() directly into other modules to programmatically fire orders without the CLI.




def generate_random_company_name(friend_names=None, words=None):
    friend_names = friend_names or DEFAULT_FRIEND_NAMES
    words = words or DEFAULT_WORDS

    selected_names = random.sample(friend_names, k=random.randint(1, min(2, len(friend_names))))
    selected_word = random.choice(words)
    suffix = random.choice(["Works", "Factory", "Industries", "Labs", "Dynamics", "Dump", "Brothel", "Empire", "Moms", "Stripclub"])

    return f"{'-'.join(selected_names)} {selected_word} {suffix}"

def random_order():
    return Order(
        type=random.choice(VALID_TYPES),
        quantity=random.randint(1, 5),
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

def generate_every_piece_client_order():
    return ClientOrder(
        name=generate_random_company_name(),
        NIF=random.randint(100000000, 999999999),
        OrderID=random.randint(1, 1000),
        orders=[
            Order(
                type=piece_type,
                quantity=1,
                DDate=random.randint(5, 30),
                Penalty=random.randint(50, 500)
            )
            for piece_type in VALID_TYPES
        ]
    )

def manual_client_order():
    name = input("Enter company name: ")
    NIF = int(input("Enter NIF (9 digits): "))
    OrderID = int(input("Enter OrderID (1-1000): "))
    orders = []

    while True:
        type = input(f"Enter order type ({', '.join(VALID_TYPES)}): ")
        quantity = int(input("Enter quantity: "))
        DDate = int(input("Enter DDate: "))
        Penalty = int(input("Enter Penalty: "))
        orders.append(Order(type, quantity, DDate, Penalty))

        cont = input("Add another order? (y/n): ")
        if cont.lower() != 'y':
            break

    return ClientOrder(name, NIF, OrderID, orders)

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
    parser = argparse.ArgumentParser(description="Generate and send client orders")
    parser.add_argument('-r', '--random', action='store_true', help='Generate a random order')
    parser.add_argument('-m', '--manual', action='store_true', help='Manually enter an order')
    parser.add_argument('-e', '--every-piece', action='store_true', help='Generate one order for each valid piece type with quantity 1')
    parser.add_argument('--host', default='localhost', help='Server host (default: localhost)')
    parser.add_argument('--port', type=int, default=6666, help='Server port (default: 6666)')
    
    args = parser.parse_args()
    
    if args.random:
        order = generate_random_client_order()
        print(f"{Fore.GREEN}Generated random order:{Style.RESET_ALL} {order}")
        send_client_order(order, host=args.host, port=args.port)
    elif args.manual:
        order = manual_client_order()
        print(f"{Fore.GREEN}Created manual order:{Style.RESET_ALL} {order}")
        send_client_order(order, host=args.host, port=args.port)
    elif args.every_piece:
        order = generate_every_piece_client_order()
        print(f"{Fore.GREEN}Generated every-piece order:{Style.RESET_ALL} {order}")
        send_client_order(order, host=args.host, port=args.port)
    else:
        while True:
            choice = input("Choose mode: random (1/r), manual (2/m), every-piece qty=1 (3/e), quit (q): ")
            match choice.lower():
                case '1' | 'r':
                    order = generate_random_client_order()
                    print(f"{Fore.GREEN}Generated random order:{Style.RESET_ALL} {order}")
                    send_client_order(order, host=args.host, port=args.port)
                case '2' | 'm':
                    order = manual_client_order()
                    print(f"{Fore.GREEN}Created manual order:{Style.RESET_ALL} {order}")
                    send_client_order(order, host=args.host, port=args.port)
                case '3' | 'e':
                    order = generate_every_piece_client_order()
                    print(f"{Fore.GREEN}Generated every-piece order:{Style.RESET_ALL} {order}")
                    send_client_order(order, host=args.host, port=args.port)
                case 'q':
                    print("Exiting...")
                    break
                case _:
                    print(f"{Fore.RED}Invalid choice, please enter '1/2/3', 'r/m/e', or 'q'{Style.RESET_ALL}")

