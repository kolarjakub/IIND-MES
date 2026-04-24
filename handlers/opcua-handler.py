from opcua import Client
import time
import sys


def get_child_by_name(node, name):
    for child in node.get_children():
        if child.get_browse_name().Name == name:
            return child
    try:
        return node.get_child([f"4:{name}"])
    except:
        pass
    available = [c.get_browse_name().to_string() for c in node.get_children()]
    raise Exception(f"Child '{name}' not found. Available: {available}")


class Node_T():
    def __init__(self, client, node):
        self.client = client
        self.node = node
        self.value = None

    def read(self):
        self.value = self.node.get_value()


class Operation_T():
    def __init__(self, client, node):
        self.client = client
        self.node = node
        self.Tool = Node_T(client, get_child_by_name(node, "Tool"))
        self.OpTime = Node_T(client, get_child_by_name(node, "OpTime"))

    def read(self):
        self.Tool.read()
        self.OpTime.read()


class Operations_T():
    def __init__(self, client, node):
        self.client = client
        self.node = node
        self.operations = [
            Operation_T(client, get_child_by_name(node, f"Operations[{i}]"))
            for i in range(11)
        ]

    def read(self):
        for op in self.operations:
            op.read()


class Workpiece_T():
    def __init__(self, client, node):
        self.client = client
        self.node = node
        self.InitPiece = Node_T(client, get_child_by_name(node, "InitPiece"))
        self.Operations = Operations_T(client, get_child_by_name(node, "Operations"))
        self.Next_Operation = Node_T(client, get_child_by_name(node, "Next_Operation"))
        self.Last_Operation = Node_T(client, get_child_by_name(node, "Last_Operation"))

    def read(self):
        self.InitPiece.read()
        self.Operations.read()
        self.Next_Operation.read()
        self.Last_Operation.read()


class Conv2Conv_fwd_T():
    def __init__(self, client, node):
        self.client = client
        self.node = node
        self.recv_cmd = Node_T(client, get_child_by_name(node, "recv_cmd"))
        self.Workpiece = Workpiece_T(client, get_child_by_name(node, "Workpiece"))

    def read(self):
        self.recv_cmd.read()
        self.Workpiece.read()


class Conv2Conv_bwd_T():
    def __init__(self, client, node):
        self.client = client
        self.node = node
        self.free_cmd = Node_T(client, get_child_by_name(node, "free_cmd"))

    def read(self):
        self.free_cmd.read()


class OPCUA_Handler():
    def __init__(
        self,
        Server_url="opc.tcp://127.0.0.1:4840",
        RootNodeId="ns=4;s=|var|CODESYS Control Win V3 x64.Application.PLC_PRG",
        listOf_Conv2Conv_fwd_T=None,
        listOf_Conv2Conv_bwd_T=None,
        listOf_Workpiece_T=None
    ):
        self.server_url = Server_url
        self.client = Client(Server_url)

        listOf_Conv2Conv_fwd_T = listOf_Conv2Conv_fwd_T or []
        listOf_Conv2Conv_bwd_T = listOf_Conv2Conv_bwd_T or []
        listOf_Workpiece_T = listOf_Workpiece_T or []

        self.client.connect()

        self.root = self.client.get_node(RootNodeId)

        for name in listOf_Conv2Conv_fwd_T:
            node = get_child_by_name(self.root, name)
            self.__dict__[name] = Conv2Conv_fwd_T(self.client, node)

        for name in listOf_Conv2Conv_bwd_T:
            node = get_child_by_name(self.root, name)
            self.__dict__[name] = Conv2Conv_bwd_T(self.client, node)

        for name in listOf_Workpiece_T:
            node = get_child_by_name(self.root, name)
            self.__dict__[name] = Workpiece_T(self.client, node)

    def read(self):
        for obj in self.__dict__.values():
            if hasattr(obj, "read"):
                obj.read()

    def disconnect(self):
        self.client.disconnect()


def dump(obj, indent=0):
    pad = "  " * indent

    if hasattr(obj, "value"):
        print(f"{pad}{obj.__class__.__name__}: {obj.value}")

    for attr_name in dir(obj):
        if attr_name.startswith("_"):
            continue

        attr = getattr(obj, attr_name)

        if hasattr(attr, "read"):
            print(f"{pad}{attr_name}:")
            dump(attr, indent + 1)

        elif isinstance(attr, list):
            print(f"{pad}{attr_name}:")
            for i, item in enumerate(attr):
                print(f"{pad}  [{i}]")
                dump(item, indent + 2)


def read_codesys_variables():
    system = None
    try:
        ConFwd = ["MES_load_Cin0", "MES_load_Cin1","MES_load_Cin2",  "MES_load_Cin4"] #"MES_load_Cin3",  -- not working

        system = OPCUA_Handler(listOf_Conv2Conv_fwd_T=ConFwd)

        while True:
            system.read()
            dump(system)
            time.sleep(5)

    except KeyboardInterrupt:
        if system:
            system.disconnect()
        sys.exit(0)

    except Exception as e:
        print(f"An error occurred: {e}")
        if system:
            system.disconnect()
        sys.exit(1)


if __name__ == "__main__":
    read_codesys_variables()