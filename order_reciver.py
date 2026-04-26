import socket
import time
import argparse
from colorama import init, Fore, Style
init(autoreset=True)

class OrderReceiver:
    def __init__(self, host='localhost', port=6666, accept_timeout=10, client_timeout=5):
        self.host = host
        self.port = port
        self.accept_timeout = accept_timeout
        self.client_timeout = client_timeout
        self.sock = None

    def start_server(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind((self.host, self.port))
        self.sock.listen(5)
        self.sock.settimeout(self.accept_timeout)
        print(Fore.GREEN + f"Order Receiver started on {self.host}:{self.port}")
        print(Fore.BLUE + f"Waiting for orders with timeout: {self.accept_timeout}s")

    def receive_orders(self, max_idle_time=None):
        idle_started = time.time()
        while True:
            try:
                client_sock, addr = self.sock.accept()
            except socket.timeout:
                print(Fore.MAGENTA + f"No incoming orders in the last {self.accept_timeout}s")
                if max_idle_time is not None and (time.time() - idle_started) >= max_idle_time:
                    print(Fore.RED + f"Idle timeout reached ({max_idle_time}s). Stopping receiver.")
                    break
                continue

            print(Fore.CYAN + f"Connection from {addr}")
            client_sock.settimeout(self.client_timeout)
            try:
                data = client_sock.recv(1024).decode('utf-8')
                if data:
                    print(Fore.YELLOW + f"Received order: {data}")
                    # Here you can add code to process the order
                    idle_started = time.time()
            except socket.timeout:
                print(Fore.RED + f"Connection {addr} timed out while receiving data")
            finally:
                client_sock.close()

    def stop_server(self):
        if self.sock:
            self.sock.close()
            print(Fore.RED + "Order Receiver stopped.")


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
        receiver.stop_server()
    finally:
        receiver.stop_server()

if __name__ == "__main__":
    main()