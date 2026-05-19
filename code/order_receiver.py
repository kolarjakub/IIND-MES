import socket
import time
import argparse
import json
from colorama import init, Fore, Style
from orders import ClientOrder, Order, VALID_TYPES

init(autoreset=True)
# OrderReceiver listens on a TCP socket (default port 6666) for incoming JSON-encoded client orders.
# When a connection arrives, it reads all data in chunks, parses it into a ClientOrder object, and increments an order counter.
# If a callback function (on_order_received) was provided at creation, it is called with the parsed order — allowing the DB handler to plug in its own logic.
# The server handles timeouts gracefully (prints a warning every 10s if idle) and exits cleanly on Ctrl+C.
# Run directly via CLI with --host, --port, --accept-timeout flags; or import OrderReceiver and use it programmatically with a callback.

class OrderReceiver:
    def __init__(self, host='localhost', port=6666, accept_timeout=10, client_timeout=5, on_order_received=None):
        self.host = host
        self.port = port
        self.accept_timeout = accept_timeout
        self.client_timeout = client_timeout
        self.sock = None

        self.orders_received = 0
        self.on_order_received = on_order_received  # Optional callback for when an order is received
        self.idle_started = time.time()

        if on_order_received is None:
            print(Fore.YELLOW + "No order received callback provided. Orders will be counted but not processed.")
            self.on_order_received = []
        elif callable(on_order_received):
            self.on_order_received = [on_order_received]
        else:
            self.on_order_received = list(on_order_received)  # Assume it's an iterable of callables
            

    def start_server(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))
        self.sock.listen(5)
        self.sock.settimeout(self.accept_timeout)
        print(Fore.GREEN + f"Order Receiver started on {self.host}:{self.port}")
        print(Fore.BLUE + f"Waiting for orders with timeout: {self.accept_timeout}s")

    def receive_orders(self, max_idle_time=None):
        while True:
            try:
                client_sock, addr = self.sock.accept()
            except socket.timeout:
                print(Fore.MAGENTA + f"No incoming orders in the last {self.accept_timeout}s")
                if max_idle_time is not None and (time.time() - self.idle_started) >= max_idle_time:
                    print(Fore.RED + f"Idle timeout reached ({max_idle_time}s). Stopping receiver.")
                    break
                continue
            except KeyboardInterrupt:
                print(Fore.YELLOW + "\nShutting down gracefully...")
                break

            print(Fore.CYAN + f"Connection from {addr}")
            client_sock.settimeout(self.client_timeout)
            try:
                chunks = []
                while True:
                    chunk = client_sock.recv(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
                data = b"".join(chunks).decode('utf-8')

                if data:
                    print(Fore.YELLOW + f"Received raw data from {addr}")
                    try:
                        order = ClientOrder.from_dict(json.loads(data))
                        print(Fore.GREEN + f"Processed order {order.OrderID} from {order.name}")
                        self.orders_received += 1
                        self.idle_started = time.time()
                        if self.on_order_received:
                            for callback in self.on_order_received:
                                callback(order)
                    except json.JSONDecodeError:
                        print(Fore.RED + f"Failed to decode JSON from {addr}")
                    except (KeyError, TypeError) as e:
                        print(Fore.RED + f"Invalid order structure: {e}")
            except socket.timeout:
                print(Fore.RED + f"Connection {addr} timed out while receiving data")
            finally:
                client_sock.close()

    def stop_server(self):
        if self.sock:
            self.sock.close()
            print(Fore.RED + "Order Receiver stopped.")
    
    def get_orders_received(self):
        return self.orders_received


def main():
    parser = argparse.ArgumentParser(description="Start order receiver server")
    parser.add_argument('--host', default='localhost', help='Server host (default: localhost)')
    parser.add_argument('--port', type=int, default=6666, help='Server port (default: 6666)')
    parser.add_argument('--accept-timeout', type=int, default=10, help='Seconds to wait for a new connection')
    parser.add_argument('--client-timeout', type=int, default=5, help='Seconds to wait for client data')
    parser.add_argument('--max-idle-time', type=int, default=None, help='Optional max idle seconds before exit')
    args = parser.parse_args()

    receiver = OrderReceiver(
        host=args.host,
        port=args.port,
        accept_timeout=args.accept_timeout,
        client_timeout=args.client_timeout,
    )
    try:
        receiver.start_server()
        receiver.receive_orders(max_idle_time=args.max_idle_time)
    except KeyboardInterrupt:
        print(Fore.YELLOW + "Shutting down...")
    finally:
        receiver.stop_server()

if __name__ == "__main__":
    main()