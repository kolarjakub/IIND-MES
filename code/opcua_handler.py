from opcua import Client, ua
import time
import sys
import logging
from dataclasses import dataclass, field
from typing import Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# OPC UA Node path helpers
# ──────────────────────────────────────────────
SERVER_URL      = "opc.tcp://127.0.0.1:4840"
APP_PREFIX      = "|var|CODESYS Control Win V3 x64.Application"
GVL_PREFIX      = f"{APP_PREFIX}.GVL"
SCADA_PREFIX    = f"{APP_PREFIX}.SCADA_PRG"
NS              = 4   # default namespace for CODESYS variables

def _node_id(path: str) -> str:
    return f"ns={NS};s={path}"


# ──────────────────────────────────────────────
# Python mirror of the PLC structs (read-only use)
# ──────────────────────────────────────────────
@dataclass
class PieceTracking:
    State_Step:     int   = 0   # E_StepState enum value
    Conveyor:       bool  = False
    ID_Piece:       int   = 0
    ID_Procedure:   int   = 0
    Piece_Material: int   = 0   # E_Material enum value
    Piece_Type:     int   = 0   # E_PieceType enum value
    Cell_1_conv:    int   = 0   # E_Location enum value
    Cell_2_conv:    int   = 0

@dataclass
class ProcedureMES:
    Status:             int   = 0   # E_Procedure_Status enum value
    ID_Procedure:       int   = 0
    Successfully_Abort: bool  = False

@dataclass
class ErrorLog:
    Error_Code:   int = 0
    Slot_Index:   int = 0
    ID_Procedure: int = 0

@dataclass
class WarehouseInventory:
    N_Pieces_W1: int = 0
    N_Pieces_W2: int = 0

@dataclass
class WorkStationTracking:
    """Flattened view of ST_WorkStation_Tracking_MES for all 4 cells."""
    data: dict = field(default_factory=dict)


# ──────────────────────────────────────────────
# Main handler class
# ──────────────────────────────────────────────
class OPCUAHandler:
    """
    Connects to the CODESYS OPC UA server and exposes clean read/write
    methods for all MES-relevant GVL variables.

    Usage
    -----
        handler = OPCUAHandler()
        handler.connect()

        # Write a recipe
        handler.write_recipe(recipe_slots)      # list of dicts

        # Trigger the PLC to load it
        handler.set_read_recipes(True)

        # Poll status
        success = handler.read_success()
        procedures = handler.read_procedures()
        inventory  = handler.read_warehouse_inventory()

        handler.disconnect()
    """

    def __init__(
        self,
        server_url: str = SERVER_URL,
        username: Optional[str] = None,
        password: Optional[str] = None,
        reconnect_delay: float = 5.0,
    ):
        self.server_url      = server_url
        self.username        = username
        self.password        = password
        self.reconnect_delay = reconnect_delay
        self._client: Optional[Client] = None

    # ── Connection management ──────────────────
    def connect(self) -> bool:
        try:
            self._client = Client(self.server_url)
            if self.username and self.password:
                self._client.set_user(self.username)
                self._client.set_password(self.password)
                logger.info(f"Connecting as user '{self.username}'...")
            self._client.connect()
            logger.info(f"Connected to OPC UA server at {self.server_url}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect: {e}")
            self._client = None
            return False

    def disconnect(self):
        if self._client:
            try:
                self._client.disconnect()
                logger.info("Disconnected from OPC UA server.")
            except Exception:
                pass
            self._client = None

    def reconnect(self) -> bool:
        logger.warning("Attempting reconnect…")
        self.disconnect()
        time.sleep(self.reconnect_delay)
        return self.connect()

    def _ensure_connected(self):
        if self._client is None:
            raise ConnectionError("Not connected. Call connect() first.")

    # ── Low-level helpers ──────────────────────
    def _get_node(self, path: str):
        self._ensure_connected()
        return self._client.get_node(_node_id(path))

    def _read(self, path: str):
        return self._get_node(path).get_value()

    def _write(self, path: str, value, variant_type: ua.VariantType = None):
        node = self._get_node(path)
        if variant_type:
            node.set_value(ua.Variant(value, variant_type))
        else:
            node.set_value(value)

    # ── MES handshake variables ────────────────

    def read_success(self) -> bool:
        """MES_Success — FALSE means recipe was rejected by PLC."""
        return bool(self._read(f"{GVL_PREFIX}.MES_Success"))

    def read_num_errors(self) -> int:
        """MES_Num_Errors — how many entries in the error log are valid."""
        return int(self._read(f"{GVL_PREFIX}.MES_Num_Errors"))

    def set_read_recipes(self, trigger: bool = True):
        """
        MES_Read_Recipes — set TRUE to tell PLC to load MES_Recipe.
        PLC resets it to FALSE when done.
        """
        self._write(f"{GVL_PREFIX}.MES_Read_Recipes", trigger, ua.VariantType.Boolean)
        logger.info(f"MES_Read_Recipes set to {trigger}")

    def wait_for_recipe_ack(self, timeout: float = 10.0, poll_interval: float = 0.2) -> bool:
        """
        Block until PLC resets MES_Read_Recipes back to FALSE (recipe loaded)
        or timeout expires.  Returns True on success.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self._read(f"{GVL_PREFIX}.MES_Read_Recipes"):
                logger.info("PLC acknowledged recipe load.")
                return True
            time.sleep(poll_interval)
        logger.warning("Timeout waiting for recipe acknowledgement.")
        return False

    # ── Recipe (write to PLC) ──────────────────

    def write_recipe_slot(self, slot_index: int, procedure: dict):
        """
        Write a single ST_Procedure slot inside MES_Recipe.Slots[slot_index].

        procedure dict keys (all optional, defaults to 0/False):
          Cell          : int  (E_Location enum)
          Status        : int  (E_Procedure_Status enum)
          ID_Procedure  : int
          ...
        """
        base = f"{GVL_PREFIX}.MES_Recipe.Slots[{slot_index}]"
        for field_name, variant_type, default in [
            ("Cell",         ua.VariantType.Int32,  0),
            ("Status",       ua.VariantType.Int32,  0),
            ("ID_Procedure", ua.VariantType.UInt32, 0),
        ]:
            value = procedure.get(field_name, default)
            try:
                self._write(f"{base}.{field_name}", value, variant_type)
            except Exception as e:
                logger.warning(f"Could not write {base}.{field_name}: {e}")

    def write_recipe(self, slots: list[dict]):
        """Write a full recipe (list of procedure dicts) to MES_Recipe.Slots."""
        for i, slot in enumerate(slots):
            self.write_recipe_slot(i, slot)
        logger.info(f"Wrote {len(slots)} recipe slots.")

    # ── Procedures (read from PLC) ─────────────

    def read_procedures(self, max_slots: int = 10) -> list[ProcedureMES]:
        """
        Read MES_Procedures array.  Reads up to max_slots entries.
        Returns a list of ProcedureMES objects.
        """
        results = []
        base = f"{GVL_PREFIX}.MES_Procedures"
        for i in range(max_slots):
            try:
                status       = int(self._read(f"{base}[{i}].Status"))
                id_proc      = int(self._read(f"{base}[{i}].ID_Procedure"))
                abort        = bool(self._read(f"{base}[{i}].Successfully_Abort"))
                results.append(ProcedureMES(status, id_proc, abort))
            except Exception:
                break   # end of accessible array
        return results

    # ── Errors (read from PLC) ─────────────────

    def read_errors(self) -> list[ErrorLog]:
        """
        Read MES_Errors up to MES_Num_Errors entries.
        Returns a list of ErrorLog objects.
        """
        n = self.read_num_errors()
        errors = []
        base = f"{GVL_PREFIX}.MES_Errors"
        for i in range(n):
            try:
                code     = int(self._read(f"{base}[{i}].Error_Code"))
                slot     = int(self._read(f"{base}[{i}].Slot_Index"))
                id_proc  = int(self._read(f"{base}[{i}].ID_Procedure"))
                errors.append(ErrorLog(code, slot, id_proc))
            except Exception as e:
                logger.warning(f"Error reading error slot {i}: {e}")
                break
        return errors

    # ── Warehouse inventory ────────────────────

    def read_warehouse_inventory(self) -> WarehouseInventory:
        """Read MES_Warehouse_Inventory (piece counts in W1 and W2)."""
        base = f"{GVL_PREFIX}.MES_Warehouse_Inventory"
        n_w1 = int(self._read(f"{base}.N_Pieces_W1"))
        n_w2 = int(self._read(f"{base}.N_Pieces_W2"))
        return WarehouseInventory(N_Pieces_W1=n_w1, N_Pieces_W2=n_w2)

    # ── Piece tracking (route steps) ──────────

    def _read_piece_tracking(self, node_path: str) -> PieceTracking:
        p = PieceTracking()
        p.State_Step     = int(self._read(f"{node_path}.State_Step"))
        p.Conveyor       = bool(self._read(f"{node_path}.Conveyor"))
        p.ID_Piece       = int(self._read(f"{node_path}.ID_Piece"))
        p.ID_Procedure   = int(self._read(f"{node_path}.ID_Procedure"))
        p.Piece_Material = int(self._read(f"{node_path}.Piece_Material"))
        p.Piece_Type     = int(self._read(f"{node_path}.Piece_Type"))
        p.Cell_1_conv    = int(self._read(f"{node_path}.Cell_1_conv"))
        p.Cell_2_conv    = int(self._read(f"{node_path}.Cell_2_conv"))
        return p

    def read_route_steps_cell(self, cell: int) -> dict:
        """
        Read MES_Route_Steps_C<cell> (cell = 1..4).
        Returns dict with 'W1_C' (PieceTracking) and 'C_W2' (list of PieceTracking).
        """
        assert 1 <= cell <= 4
        base = f"{GVL_PREFIX}.MES_Route_Steps_C{cell}"
        result = {}
        result["W1_C"] = self._read_piece_tracking(f"{base}.W1_C")
        result["C_W2"] = []
        for i in range(5):
            try:
                result["C_W2"].append(self._read_piece_tracking(f"{base}.C_W2[{i}]"))
            except Exception:
                break
        return result

    def read_route_steps_T(self) -> dict:
        """Read MES_Route_Steps_T (W2_T and T_W1 array)."""
        base = f"{GVL_PREFIX}.MES_Route_Steps_T"
        result = {}
        result["W2_T"] = self._read_piece_tracking(f"{base}.W2_T")
        result["T_W1"] = []
        for i in range(5):
            try:
                result["T_W1"].append(self._read_piece_tracking(f"{base}.T_W1[{i}]"))
            except Exception:
                break
        return result

    def read_route_steps_L(self) -> PieceTracking:
        """Read MES_Route_Steps_L (single ST_Piece_Tracking)."""
        return self._read_piece_tracking(f"{GVL_PREFIX}.MES_Route_Steps_L")

    # ── Tool tracking ──────────────────────────

    def read_tool_track(self, cell: int) -> dict:
        """
        Read MES_Tool_Track_C<cell>.
        Returns dict with Work_Station_1/2/3 enum int values.
        """
        assert 1 <= cell <= 4
        base = f"{GVL_PREFIX}.MES_Tool_Track_C{cell}"
        return {
            "Work_Station_1": int(self._read(f"{base}.Work_Station_1")),
            "Work_Station_2": int(self._read(f"{base}.Work_Station_2")),
            "Work_Station_3": int(self._read(f"{base}.Work_Station_3")),
        }

    def read_tool_change_track(self, cell: int) -> dict:
        """Read MES_Tool_Change_Track_C<cell>. Returns dict with Station_1/2/3 bools."""
        assert 1 <= cell <= 4
        base = f"{GVL_PREFIX}.MES_Tool_Change_Track_C{cell}"
        return {
            "Station_1": bool(self._read(f"{base}.Station_1")),
            "Station_2": bool(self._read(f"{base}.Station_2")),
            "Station_3": bool(self._read(f"{base}.Station_3")),
        }

    def read_work_track(self, cell: int) -> dict:
        """Read MES_Work_Track_C<cell> (workstation busy/done flags)."""
        assert 1 <= cell <= 4
        base = f"{GVL_PREFIX}.MES_Work_Track_C{cell}"
        return {
            "Working_Station_1":   bool(self._read(f"{base}.Working_Station_1")),
            "Work_Done_Station_1": bool(self._read(f"{base}.Work_Done_Station_1")),
            "Working_Station_2":   bool(self._read(f"{base}.Working_Station_2")),
            "Work_Done_Station_2": bool(self._read(f"{base}.Work_Done_Station_2")),
            "Working_Station_3":   bool(self._read(f"{base}.Working_Station_3")),
            "Work_Done_Station_3": bool(self._read(f"{base}.Work_Done_Station_3")),
            "N_Legs_3":            int(self._read(f"{base}.N_Legs_3")),
            "N_Tops_3":            int(self._read(f"{base}.N_Tops_3")),
        }

    # ── Convenience: snapshot of everything ───

    def read_full_status(self) -> dict:
        """
        Returns a dict snapshot of all MES-relevant readable variables.
        Useful for logging/DB storage.
        """
        status = {
            "success":            self.read_success(),
            "num_errors":         self.read_num_errors(),
            "errors":             self.read_errors(),
            "warehouse":          self.read_warehouse_inventory(),
            "procedures":         self.read_procedures(),
            "route_steps_L":      self.read_route_steps_L(),
            "route_steps_T":      self.read_route_steps_T(),
        }
        for c in range(1, 5):
            status[f"route_steps_C{c}"]      = self.read_route_steps_cell(c)
            status[f"tool_track_C{c}"]        = self.read_tool_track(c)
            status[f"tool_change_track_C{c}"] = self.read_tool_change_track(c)
            status[f"work_track_C{c}"]        = self.read_work_track(c)
        return status


# ──────────────────────────────────────────────
# Quick self-test (run directly)
# ──────────────────────────────────────────────
if __name__ == "__main__":
    import getpass
    username = input("CODESYS OPC UA username (leave blank for anonymous): ").strip() or None
    password = getpass.getpass("Password: ").strip() if username else None

    handler = OPCUAHandler(username=username, password=password)

    if not handler.connect():
        print("Could not connect to OPC UA server. Is CODESYS running?")
        sys.exit(1)

    try:
        while True:
            print("\n--- MES Status Snapshot ---")
            print(f"  Success flag  : {handler.read_success()}")
            print(f"  Num errors    : {handler.read_num_errors()}")
            inv = handler.read_warehouse_inventory()
            print(f"  Warehouse W1  : {inv.N_Pieces_W1} pieces")
            print(f"  Warehouse W2  : {inv.N_Pieces_W2} pieces")
            errors = handler.read_errors()
            if errors:
                print(f"  Errors        : {errors}")
            time.sleep(2)

    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        handler.disconnect()