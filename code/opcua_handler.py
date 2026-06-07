"""
opcua_handler.py
================
Low-level OPC-UA handler for CODESYS.

RECIPE STRUCTURE (confirmed from PLC v4.1 Final_Test code):
===========================================================
Each piece uses EXACTLY 13 slots (indices 0-12), one ID_Procedure per slot.

  [0]  Load piece 0      Cell=L,  Tool=IDLE
  [1]  Load piece 1      Cell=L,  Tool=IDLE
  [2]  Load piece 2      Cell=L,  Tool=IDLE
  [3]  Machine piece 0   Cell=C?, Tool=T?,  Tool_Time_Sec=?
  [4]  Machine piece 1   Cell=C?, Tool=T?,  Tool_Time_Sec=?
  [5]  Machine piece 2   Cell=C?, Tool=T?,  Tool_Time_Sec=?
  [6]  Transport piece 0 Cell=T,  Tool=IDLE, For_Assembly=TRUE
  [7]  Transport piece 1 Cell=T,  Tool=IDLE, For_Assembly=TRUE
  [8]  Transport piece 2 Cell=T,  Tool=IDLE, For_Assembly=TRUE
  [9]  Assembly Leg 1    Cell=C?, Tool=T8/T9, IDs_Assembly.Leg_1=ID
  [10] Assembly Leg 2    Cell=C?, Tool=T8/T9, IDs_Assembly.Leg_2=ID
  [11] Assembly Top      Cell=C?, Tool=T8/T9, IDs_Assembly.Top=ID
  [12] Unload            Cell=U,  Tool=IDLE

Multi-cell recipes (RWM / SWM): machining slots can target different cells
via the recipe's optional `mach_cells` list.  Single-cell recipes omit it
and all machining uses the same `cell` as assembly.

Cell <-> product mapping (confirmed from Final_Test PLC XML v4.1):
  C1 [T1,T2,T3,T8,T9,T11] -> RWW assembly  (all-wood round)
  C2 [T1,T2,T3,T8,T9,T10] -> SWW assembly  (all-wood square)
  C3 [T4,T5,T6,T8,T9,T11] -> RMM assembly  (all-metal round)
                           -> RWM assembly  (mixed round, T9)
  C4 [T4,T5,T6,T8,T9,T10] -> SMM assembly  (all-metal square)
                           -> SWM assembly  (mixed square, T9)

Assembly tools:
  T8 = same-material assembly  (RWW, SWW, RMM, SMM)
  T9 = mixed-material assembly (RWM, SWM) -- confirmed Final_Test slots 9-11

Confirmed enum values (PLC XML):
  E_Location : L=30, T=20, U=40, W1=50, W2=60,
               C1=100, C2=200, C3=300, C4=400
  E_Tool     : IDLE=0, T1=1, T2=2, T3=3, T4=4, T5=5, T6=6,
               T8=8, T9=9, T10=10, T11=11
  E_Material : WOOD=1, METAL=2
  E_PieceType: IDLE=0, WOOD_ROUND_TOP=3, WOOD_SQUARE_TOP=4, WOOD_LEG=5,
               METAL_ROUND_TOP=6, METAL_SQUARE_TOP=7, METAL_LEG=8,
               WOOD_ROUND_TABLE=9, WOOD_SQUARE_TABLE=10,
               WOOD_ROUND_TOP_METAL_LEGS=11, WOOD_SQUARE_TOP_METAL_LEGS=12,
               METAL_ROUND_TABLE=13, METAL_SQUARE_TABLE=14
  E_Procedure_Status: IDLE=0, NEW_ORDER=1, EXECUTION=2, STOPPED=3, COMPLETED=4

MAX_LOGIC_N_PROCEDURES: 150 (from Final_Test).
"""

from opcua import Client, ua
import time
import logging

logger = logging.getLogger(__name__)

SERVER_URL           = "opc.tcp://127.0.0.1:4840"
GVL_BASE             = "ns=4;s=|var|CODESYS Control Win V3 x64.Application.GVL."
RECIPE_POLL_INTERVAL = 0.2
RECIPE_ACK_TIMEOUT   = 15.0


# -- Enum mirrors (confirmed from PLC XML) ------------------------------------

class ELocation:
    IDLE = 0
    T    = 20   # transport conveyor
    L    = 30   # loading station
    U    = 40   # unloading station
    W1   = 50   # raw-material warehouse
    W2   = 60   # finished-goods warehouse
    C1   = 100
    C2   = 200
    C3   = 300
    C4   = 400

class ETool:
    IDLE = 0
    T1   = 1    # Wood Round Top machining
    T2   = 2    # Wood Square Top machining
    T3   = 3    # Wood Leg machining
    T4   = 4    # Metal Round Top machining
    T5   = 5    # Metal Leg machining
    T6   = 6    # Metal Square Top machining
    T8   = 8    # Same-material assembly  (RWW / SWW / RMM / SMM)
    T9   = 9    # Mixed-material assembly (RWM / SWM)
    T10  = 10   # Wood Leg alternative    (available in C2 / C4)
    T11  = 11   # Metal Leg alternative   (available in C1 / C3)

class EMaterial:
    WOOD  = 1
    METAL = 2

class EPieceType:
    IDLE                       = 0
    WOOD_ROUND_TOP             = 3
    WOOD_SQUARE_TOP            = 4
    WOOD_LEG                   = 5
    METAL_ROUND_TOP            = 6
    METAL_SQUARE_TOP           = 7
    METAL_LEG                  = 8
    WOOD_ROUND_TABLE           = 9    # RWW
    WOOD_SQUARE_TABLE          = 10   # SWW
    WOOD_ROUND_TOP_METAL_LEGS  = 11   # RWM
    WOOD_SQUARE_TOP_METAL_LEGS = 12   # SWM
    METAL_ROUND_TABLE          = 13   # RMM
    METAL_SQUARE_TABLE         = 14   # SMM

class EProcedureStatus:
    IDLE      = 0
    NEW_ORDER = 1
    EXECUTION = 2
    STOPPED   = 3
    COMPLETED = 4


# -- Product recipe definitions -----------------------------------------------
#
# Required fields:
#   cell           - assembly cell (also default machining cell for single-cell)
#   mach_cells     - list[3] of per-piece machining cells, or None = use `cell`
#   materials      - list[3] of EMaterial for raw pieces (order: leg1, leg2, top)
#   raw_types      - list[3] of EPieceType at load time
#   mach_tools     - list[3] of ETool for machining
#   mach_times     - list[3] of int seconds for machining
#   mach_types     - list[3] of EPieceType after machining
#   asm_tool       - ETool for all three assembly slots (T8=same-mat, T9=mixed)
#   asm_time       - int seconds per assembly step
#   final_type     - EPieceType of the assembled product
#   final_material - EMaterial written to the unload slot (WOOD for RWM/SWM)
#   asm_leg1_idx   - index (0-2) of the first-leg piece
#   asm_leg2_idx   - index (0-2) of the second-leg piece
#   asm_top_idx    - index (0-2) of the top piece

PRODUCT_RECIPES = {

    # -- RWW: Wood Round Table -- assembly C1 ---------------------------------
    # Legs: T3 (10s each)  Top: T1 (30s)  Assembly: T8
    "RWW": {
        "cell":           ELocation.C1,
        "mach_cells":     None,
        "materials":      [EMaterial.WOOD,  EMaterial.WOOD,  EMaterial.WOOD],
        "raw_types":      [EPieceType.WOOD_LEG, EPieceType.WOOD_LEG,
                           EPieceType.WOOD_ROUND_TOP],
        "mach_tools":     [ETool.T3, ETool.T3, ETool.T1],
        "mach_times":     [10, 10, 30],
        "mach_types":     [EPieceType.WOOD_LEG, EPieceType.WOOD_LEG,
                           EPieceType.WOOD_ROUND_TOP],
        "asm_tool":       ETool.T8,
        "asm_time":       10,
        "final_type":     EPieceType.WOOD_ROUND_TABLE,
        "final_material": EMaterial.WOOD,
        "asm_leg1_idx":   0,
        "asm_leg2_idx":   1,
        "asm_top_idx":    2,
    },

    # -- SWW: Wood Square Table -- assembly C2 --------------------------------
    # Legs: T3 (10s each)  Top: T2 (20s)  Assembly: T8
    "SWW": {
        "cell":           ELocation.C2,
        "mach_cells":     None,
        "materials":      [EMaterial.WOOD,  EMaterial.WOOD,  EMaterial.WOOD],
        "raw_types":      [EPieceType.WOOD_LEG, EPieceType.WOOD_LEG,
                           EPieceType.WOOD_SQUARE_TOP],
        "mach_tools":     [ETool.T3, ETool.T3, ETool.T2],
        "mach_times":     [10, 10, 20],
        "mach_types":     [EPieceType.WOOD_LEG, EPieceType.WOOD_LEG,
                           EPieceType.WOOD_SQUARE_TOP],
        "asm_tool":       ETool.T8,
        "asm_time":       10,
        "final_type":     EPieceType.WOOD_SQUARE_TABLE,
        "final_material": EMaterial.WOOD,
        "asm_leg1_idx":   0,
        "asm_leg2_idx":   1,
        "asm_top_idx":    2,
    },

    # -- RWM: Wood Round Top + Metal Legs -- assembly C3 ----------------------
    # Metal legs: T5 at C3 (30s each)  Wood round top: T1 at C1 (30s)
    # Assembly: T9 (mixed-material) at C3
    # Unload Piece_Material=WOOD -- confirmed from Final_Test slot 12
    "RWM": {
        "cell":           ELocation.C3,
        "mach_cells":     [ELocation.C3, ELocation.C3, ELocation.C1],
        "materials":      [EMaterial.METAL, EMaterial.METAL, EMaterial.WOOD],
        "raw_types":      [EPieceType.METAL_LEG, EPieceType.METAL_LEG,
                           EPieceType.WOOD_ROUND_TOP],
        "mach_tools":     [ETool.T5, ETool.T5, ETool.T1],
        "mach_times":     [30, 30, 30],
        "mach_types":     [EPieceType.METAL_LEG, EPieceType.METAL_LEG,
                           EPieceType.WOOD_ROUND_TOP],
        "asm_tool":       ETool.T9,
        "asm_time":       10,
        "final_type":     EPieceType.WOOD_ROUND_TOP_METAL_LEGS,
        "final_material": EMaterial.WOOD,
        "asm_leg1_idx":   0,
        "asm_leg2_idx":   1,
        "asm_top_idx":    2,
    },

    # -- SWM: Wood Square Top + Metal Legs -- assembly C4 ---------------------
    # Metal legs: T5 at C4 (30s each)  Wood square top: T2 at C2 (20s)
    # Assembly: T9 (mixed-material) at C4
    # Unload Piece_Material=WOOD -- confirmed from Final_Test slot 25
    "SWM": {
        "cell":           ELocation.C4,
        "mach_cells":     [ELocation.C4, ELocation.C4, ELocation.C2],
        "materials":      [EMaterial.METAL, EMaterial.METAL, EMaterial.WOOD],
        "raw_types":      [EPieceType.METAL_LEG, EPieceType.METAL_LEG,
                           EPieceType.WOOD_SQUARE_TOP],
        "mach_tools":     [ETool.T5, ETool.T5, ETool.T2],
        "mach_times":     [30, 30, 20],
        "mach_types":     [EPieceType.METAL_LEG, EPieceType.METAL_LEG,
                           EPieceType.WOOD_SQUARE_TOP],
        "asm_tool":       ETool.T9,
        "asm_time":       10,
        "final_type":     EPieceType.WOOD_SQUARE_TOP_METAL_LEGS,
        "final_material": EMaterial.WOOD,
        "asm_leg1_idx":   0,
        "asm_leg2_idx":   1,
        "asm_top_idx":    2,
    },

    # -- RMM: Metal Round Table -- assembly C3 --------------------------------
    # Legs: T5 (30s each)  Top: T4 (35s)  Assembly: T8
    "RMM": {
        "cell":           ELocation.C3,
        "mach_cells":     None,
        "materials":      [EMaterial.METAL, EMaterial.METAL, EMaterial.METAL],
        "raw_types":      [EPieceType.METAL_LEG, EPieceType.METAL_LEG,
                           EPieceType.METAL_ROUND_TOP],
        "mach_tools":     [ETool.T5, ETool.T5, ETool.T4],
        "mach_times":     [30, 30, 35],
        "mach_types":     [EPieceType.METAL_LEG, EPieceType.METAL_LEG,
                           EPieceType.METAL_ROUND_TOP],
        "asm_tool":       ETool.T8,
        "asm_time":       10,
        "final_type":     EPieceType.METAL_ROUND_TABLE,
        "final_material": EMaterial.METAL,
        "asm_leg1_idx":   0,
        "asm_leg2_idx":   1,
        "asm_top_idx":    2,
    },

    # -- SMM: Metal Square Table -- assembly C4 -------------------------------
    # Legs: T5 (30s each)  Top: T6 (25s)  Assembly: T8
    "SMM": {
        "cell":           ELocation.C4,
        "mach_cells":     None,
        "materials":      [EMaterial.METAL, EMaterial.METAL, EMaterial.METAL],
        "raw_types":      [EPieceType.METAL_LEG, EPieceType.METAL_LEG,
                           EPieceType.METAL_SQUARE_TOP],
        "mach_tools":     [ETool.T5, ETool.T5, ETool.T6],
        "mach_times":     [30, 30, 25],
        "mach_types":     [EPieceType.METAL_LEG, EPieceType.METAL_LEG,
                           EPieceType.METAL_SQUARE_TOP],
        "asm_tool":       ETool.T8,
        "asm_time":       10,
        "final_type":     EPieceType.METAL_SQUARE_TABLE,
        "final_material": EMaterial.METAL,
        "asm_leg1_idx":   0,
        "asm_leg2_idx":   1,
        "asm_top_idx":    2,
    },
}


# -- Recipe builder -----------------------------------------------------------

def build_recipe(
    piece_type:         str,
    id_recipe:          int,
    id_procedure_start: int,
    id_piece_start:     int,
    id_final_piece:     int,
    unload_location:    int = ELocation.U,
    slot_offset:        int = 0,
) -> list:
    """
    Build the 13-slot recipe for one piece (slot indices 0-12).

    Slot groups:
      0-2  : Loading    (Cell=L)
      3-5  : Machining  (Cell from mach_cells; may differ per piece for RWM/SWM)
      6-8  : Transport  (Cell=T, For_Assembly=TRUE)
      9-11 : Assembly   (Cell = assembly cell from recipe)
      12   : Unload     (Cell = unload_location, default U=40)

    Args:
        piece_type         : one of RWW / SWW / RWM / SWM / RMM / SMM
        id_recipe          : unique recipe identifier
        id_procedure_start : first ID_Procedure value (slots increment +0..+12)
        id_piece_start     : ID of the first raw piece (id_piece_start+0/+1/+2)
        id_final_piece     : ID assigned to the assembled final product
        unload_location    : ELocation for the unload step (default U=40)

    Returns list of 13 slot dicts ready for OpcUaHandler.write_recipe().
    """
    key  = piece_type.upper()
    if key not in PRODUCT_RECIPES:
        raise ValueError(f"Unknown piece type '{key}'. "
                         f"Valid: {list(PRODUCT_RECIPES)}")

    r          = PRODUCT_RECIPES[key]
    cell       = r["cell"]
    mach_cells = r["mach_cells"] or [cell, cell, cell]
    slots      = []
    pid        = [id_piece_start, id_piece_start + 1, id_piece_start + 2]

    def _slot(**kw):
        base = {
            "N_Slot":                    slot_offset + len(slots),
            "ID_Procedure":              id_procedure_start + len(slots),
            "ID_Recipe":                 id_recipe,
            "Status":                    EProcedureStatus.NEW_ORDER,
            "Tool":                      ETool.IDLE,
            "Tool_Time_Sec":             0,
            "For_Assembly":              False,
            "ID_Assembly_Final_Product": 0,
            "IDs_Assembly_Leg_1":        0,
            "IDs_Assembly_Leg_2":        0,
            "IDs_Assembly_Top":          0,
        }
        base.update(kw)
        slots.append(base)

    # -- Loading (slots 0-2) --------------------------------------------------
    for k in range(3):
        _slot(ID_Piece       = pid[k],
              Cell           = ELocation.L,
              Piece_Material = r["materials"][k],
              Piece_Type     = r["raw_types"][k])

    # -- Machining (slots 3-5) ------------------------------------------------
    # RWM/SWM use different cells per piece; single-cell recipes use `cell` for all.
    for k in range(3):
        _slot(ID_Piece       = pid[k],
              Cell           = mach_cells[k],
              Tool           = r["mach_tools"][k],
              Tool_Time_Sec  = r["mach_times"][k],
              Piece_Material = r["materials"][k],
              Piece_Type     = r["mach_types"][k])

    # -- Transport (slots 6-8) ------------------------------------------------
    for k in range(3):
        _slot(ID_Piece       = pid[k],
              Cell           = ELocation.T,
              Piece_Material = r["materials"][k],
              Piece_Type     = r["mach_types"][k],
              For_Assembly   = True)

    # -- Assembly Leg 1 (slot 9) ----------------------------------------------
    leg1 = r["asm_leg1_idx"]
    _slot(ID_Piece                  = pid[leg1],
          Cell                      = cell,
          Tool                      = r["asm_tool"],
          Tool_Time_Sec             = r["asm_time"],
          Piece_Material            = r["materials"][leg1],
          Piece_Type                = r["final_type"],
          For_Assembly              = True,
          ID_Assembly_Final_Product = id_final_piece,
          IDs_Assembly_Leg_1        = pid[leg1])

    # -- Assembly Leg 2 (slot 10) ---------------------------------------------
    leg2 = r["asm_leg2_idx"]
    _slot(ID_Piece                  = pid[leg2],
          Cell                      = cell,
          Tool                      = r["asm_tool"],
          Tool_Time_Sec             = r["asm_time"],
          Piece_Material            = r["materials"][leg2],
          Piece_Type                = r["final_type"],
          For_Assembly              = True,
          ID_Assembly_Final_Product = id_final_piece,
          IDs_Assembly_Leg_2        = pid[leg2])

    # -- Assembly Top (slot 11) -----------------------------------------------
    top = r["asm_top_idx"]
    _slot(ID_Piece                  = pid[top],
          Cell                      = cell,
          Tool                      = r["asm_tool"],
          Tool_Time_Sec             = r["asm_time"],
          Piece_Material            = r["materials"][top],
          Piece_Type                = r["final_type"],
          For_Assembly              = True,
          ID_Assembly_Final_Product = id_final_piece,
          IDs_Assembly_Top          = pid[top])

    # -- Unload (slot 12) -----------------------------------------------------
    # Use `final_material` so mixed-material tables (RWM/SWM) correctly report
    # WOOD here, matching the value the PLC writes in Final_Test slot 12.
    _slot(ID_Piece       = id_final_piece,
          Cell           = unload_location,
          Piece_Material = r["final_material"],
          Piece_Type     = r["final_type"])

    assert len(slots) == 13, f"build_recipe: expected 13 slots, got {len(slots)}"
    return slots


# -- OpcUaHandler -------------------------------------------------------------

class OpcUaHandler:
    """Low-level CODESYS OPC-UA handler.  Only plc_interface.py imports this."""

    def __init__(self, server_url=SERVER_URL, username=None, password=None):
        self.server_url = server_url
        self.username   = username
        self.password   = password
        self.client     = Client(server_url)
        self.connected  = False

    # -- Connection -----------------------------------------------------------

    def connect(self):
        if not self.connected:
            if self.username and self.password:
                self.client.set_user(self.username)
                self.client.set_password(self.password)
            self.client.connect()
            self.connected = True
            print(f"[OpcUaHandler] Connected to {self.server_url}")

    def disconnect(self):
        if self.connected:
            try:
                self.client.disconnect()
            except Exception:
                pass
            self.connected = False
            print("[OpcUaHandler] Disconnected.")

    def _ensure_connected(self):
        if not self.connected:
            raise ConnectionError("Not connected. Call connect() first.")

    # -- Low-level helpers ----------------------------------------------------

    def _node(self, path):
        self._ensure_connected()
        return self.client.get_node(GVL_BASE + path)

    def _read(self, path):
        return self._node(path).get_value()

    def _write(self, path, value):
        """Auto-typed write: reads the node's declared variant type first."""
        try:
            node = self._node(path)
            t    = node.get_data_type_as_variant_type()
            node.set_value(ua.Variant(value, t))
            return True
        except Exception as e:
            logger.error(f"[OpcUaHandler] Write failed '{path}': {e}")
            return False

    # -- Procedure limits -----------------------------------------------------

    def write_procedure_limits(self, value=150):
        """Set all MAX_LOGIC_N_PROCEDURES fields (Final_Test uses 150)."""
        fields = [
            "MAX_LOGIC_N_PROCEDURES.RECIPE",
            "MAX_LOGIC_N_PROCEDURES.C1",
            "MAX_LOGIC_N_PROCEDURES.C2",
            "MAX_LOGIC_N_PROCEDURES.C3",
            "MAX_LOGIC_N_PROCEDURES.C4",
            "MAX_LOGIC_N_PROCEDURES.T",
            "MAX_LOGIC_N_PROCEDURES.L",
            "MAX_LOGIC_N_PROCEDURES.U",
        ]
        ok = all(self._write(f, value) for f in fields)
        if ok:
            print(f"[OpcUaHandler] Procedure limits set to {value}")
        return ok

    # -- Recipe write ---------------------------------------------------------

    def write_slot(self, slot_index: int, slot: dict) -> bool:
        """Write one ST_Procedure slot to MES_Recipe.Slots[slot_index]."""
        base = f"MES_Recipe.Slots[{slot_index}]"
        fields = [
            (f"{base}.N_Slot",                   slot["N_Slot"]),
            (f"{base}.ID_Procedure",              slot["ID_Procedure"]),
            (f"{base}.ID_Recipe",                 slot["ID_Recipe"]),
            (f"{base}.ID_Piece",                  slot["ID_Piece"]),
            (f"{base}.Status",                    slot["Status"]),
            (f"{base}.Cell",                      slot["Cell"]),
            (f"{base}.Tool",                      slot["Tool"]),
            (f"{base}.Tool_Time_Sec",             slot["Tool_Time_Sec"]),
            (f"{base}.Piece_Material",            slot["Piece_Material"]),
            (f"{base}.Piece_Type",                slot["Piece_Type"]),
            (f"{base}.For_Assembly",              slot["For_Assembly"]),
            (f"{base}.ID_Assembly_Final_Product", slot["ID_Assembly_Final_Product"]),
            (f"{base}.IDs_Assembly.Leg_1",        slot["IDs_Assembly_Leg_1"]),
            (f"{base}.IDs_Assembly.Leg_2",        slot["IDs_Assembly_Leg_2"]),
            (f"{base}.IDs_Assembly.Top",          slot["IDs_Assembly_Top"]),
        ]
        ok = True
        for path, val in fields:
            if not self._write(path, val):
                ok = False
        return ok

    def write_recipe(self, slots: list) -> bool:
        """Write all slots to MES_Recipe."""
        ok = True
        for i, slot in enumerate(slots):
            if self.write_slot(i, slot):
                print(f"[OpcUaHandler] Slot[{i:2d}] OK  "
                      f"Cell={slot['Cell']:3d}  Tool={slot['Tool']:2d}  "
                      f"ID_Proc={slot['ID_Procedure']}")
            else:
                print(f"[OpcUaHandler] Slot[{i:2d}] FAILED")
                ok = False
        return ok

    # -- Trigger --------------------------------------------------------------

    def trigger_recipe(self, timeout: float = RECIPE_ACK_TIMEOUT) -> bool:
        """
        Set MES_Read_Recipes=TRUE then poll until the PLC resets it back.
        Returns True on acknowledgement, False on timeout.
        """
        self._write("MES_Read_Recipes", True)
        print("[OpcUaHandler] Triggered -- waiting for PLC ack...")
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if not bool(self._read("MES_Read_Recipes")):
                    print("[OpcUaHandler] PLC acknowledged.")
                    return True
            except Exception:
                pass
            time.sleep(RECIPE_POLL_INTERVAL)
        print(f"[OpcUaHandler] No ack within {timeout}s -- forcing reset.")
        self._write("MES_Read_Recipes", False)
        return False

    def dispatch(self, slots: list, limits: int = 150) -> bool:
        """
        Full send sequence: set limits -> write slots -> trigger.
        Returns True if the PLC acknowledged.
        """
        if not slots:
            return False
        self.write_procedure_limits(limits)
        if not self.write_recipe(slots):
            print("[OpcUaHandler] Recipe write failed -- not triggering.")
            return False
        return self.trigger_recipe()

    # -- MES reads ------------------------------------------------------------
    def read_tool_change_tracking(self) -> dict:
        """
        Read ST_ToolChange_Tracking_MES.
        Returns a dict of {(station, sub): bool} for all 12 flags.

        Stations 1-4 = C1-C4.  Sub-stations 1-3 = the 3 workstations per cell.
        True = tool change complete / station ready.
        """
        var  = "MES_ToolChange_Tracking"   # exact GVL variable name
        result = {}
        for station in range(1, 5):        # 1..4
            for sub in range(1, 4):        # 1..3
                field = f"Station{station}_{sub}"
                try:
                    result[(station, sub)] = bool(self._read(f"{var}.{field}"))
                except Exception:
                    result[(station, sub)] = False
        print(f"[OpcUaHandler] Tool change tracking: {result}")
        return result
    def read_machine_statistics(self) -> list:
        """
        Read MES_Machine_Statistics.Machine[0..11].

        Returns list of 12 dicts:
          machine_index, operating_time (s), occupation_pct (%),
          tool_times [T1,T2,T3] (s), tool_changes, pieces_total,
          pieces_by_type [0..11]
        """
        N_MACHINES    = 12
        N_TOOLS       = 3
        N_PIECE_TYPES = 12
        result = []
        for i in range(N_MACHINES):
            base = f"MES_Machine_Statistics.Machine[{i}]"
            try:
                result.append({
                    "machine_index":  i,
                    "operating_time": float(self._read(
                        f"{base}.Total_Operating_Time")),
                    "occupation_pct": float(self._read(
                        f"{base}.Occupation_Percentage")),
                    "tool_times":     [float(self._read(
                        f"{base}.Total_Operating_Time_Tools[{j}]"))
                        for j in range(N_TOOLS)],
                    "tool_changes":   int(self._read(
                        f"{base}.Number_Of_Tool_Changes")),
                    "pieces_total":   int(self._read(
                        f"{base}.Total_Number_Of_Operated_Workpieces")),
                    "pieces_by_type": [int(self._read(
                        f"{base}.Total_Number_Of_Operated_Workpieces_Each_Type[{k}]"))
                        for k in range(N_PIECE_TYPES)],
                })
            except Exception:
                result.append({
                    "machine_index":  i, "operating_time": 0.0,
                    "occupation_pct": 0.0, "tool_times": [0.0] * N_TOOLS,
                    "tool_changes":   0,  "pieces_total": 0,
                    "pieces_by_type": [0] * N_PIECE_TYPES,
                })
        return result

    def read_success(self) -> bool:
        return bool(self._read("MES_Success"))

    def read_num_errors(self) -> int:
        return int(self._read("MES_Num_Errors"))

    def read_warehouse_inventory(self) -> dict:
        """
        Return warehouse counts.

        W1: counts by Piece_Material (raw + mid-process pieces)
        W2: counts by Type_Piece in {9..14} (finished assembled tables)

        Returns:
            {
                "W1":          int,  # PLC total for W1
                "W2":          int,  # PLC total for W2
                "W1_wood":     int,  # wood pieces in W1
                "W1_metal":    int,  # metal pieces in W1
                "W2_finished": int,  # assembled tables in W2
            }
        """
        FINISHED_TYPES = {9, 10, 11, 12, 13, 14}

        w1_total = int(self._read("MES_Warehouse_Inventory.N_Pieces_W1"))
        w2_total = int(self._read("MES_Warehouse_Inventory.N_Pieces_W2"))
        w1_wood = w1_metal = w2_finished = 0

        for i in range(min(w1_total + 2, 32)):
            try:
                base = f"MES_Warehouse_Inventory.Rast_N_Pieces_In_W1[{i}]"
                if not bool(self._read(f"{base}.Inside_Warhouse_W1")):
                    continue
                mat = int(self._read(f"{base}.Piece_Material"))
                if mat == EMaterial.WOOD:
                    w1_wood  += 1
                elif mat == EMaterial.METAL:
                    w1_metal += 1
            except Exception:
                break

        for i in range(min(w2_total + 2, 32)):
            try:
                base = f"MES_Warehouse_Inventory.Rast_N_Pieces_In_W2[{i}]"
                if not bool(self._read(f"{base}.Inside_Warhouse_W2")):
                    continue
                if int(self._read(f"{base}.Type_Piece")) in FINISHED_TYPES:
                    w2_finished += 1
            except Exception:
                break

        return {
            "W1":          w1_total,
            "W2":          w2_total,
            "W1_wood":     w1_wood,
            "W1_metal":    w1_metal,
            "W2_finished": w2_finished,
        }

    def read_procedures(self, max_slots: int = 10) -> list:
        """
        Return active procedures (status != IDLE=0 and != COMPLETED=4).
        Stops on the first slot with ID_Procedure == 0.
        """
        results = []
        for i in range(max_slots):
            try:
                id_proc = int(self._read(f"MES_Procedures[{i}].ID_Procedure"))
                if id_proc == 0:
                    break
                status  = int(self._read(f"MES_Procedures[{i}].Status"))
                aborted = bool(self._read(f"MES_Procedures[{i}].Successfully_Abort"))
                if status not in (EProcedureStatus.IDLE, EProcedureStatus.COMPLETED):
                    results.append({"id": id_proc, "status": status,
                                    "aborted": aborted})
            except Exception:
                break
        return results

    def read_cell_workstation_tracking(self, cell: str) -> dict:
        """
        Read MES_Work_Track_<cell> for cell in {"C1","C2","C3","C4"}.

        Returns:
            {
                "working":  [bool, bool, bool],  # stations 1-3 currently busy
                "done":     [bool, bool, bool],  # stations 1-3 finished
                "any_free": bool,
                "all_busy": bool,
            }
        """
        base    = f"MES_Work_Track_{cell}"
        working = []
        done    = []
        for i in range(1, 4):
            try:
                w = bool(self._read(f"{base}.Working_Station_{i}"))
                d = bool(self._read(f"{base}.Work_Done_Station_{i}"))
            except Exception:
                w, d = False, False
            working.append(w)
            done.append(d)
        return {
            "working":  working,
            "done":     done,
            "any_free": not all(working),
            "all_busy": all(working),
        }

    def read_errors(self) -> list:
        n      = self.read_num_errors()
        errors = []
        for i in range(min(n, 20)):
            try:
                errors.append({
                    "code":         int(self._read(f"MES_Errors[{i}].Error_Code")),
                    "slot":         int(self._read(f"MES_Errors[{i}].Slot_Index")),
                    "procedure_id": int(self._read(f"MES_Errors[{i}].ID_Procedure")),
                })
            except Exception:
                break
        return errors


# -- Self-test ----------------------------------------------------------------

if __name__ == "__main__":
    import getpass

    TYPES = list(PRODUCT_RECIPES.keys())
    PHASE = ["Load","Load","Load","Mach","Mach","Mach",
             "Tran","Tran","Tran","Asm1","Asm2","Asm3","Unld"]

    print("=== Recipe builder (offline) ===")
    for idx, ptype in enumerate(TYPES):
        slots = build_recipe(
            piece_type          = ptype,
            id_recipe           = idx + 1,
            id_procedure_start  = (idx + 1) * 100 + 1,
            id_piece_start      = (idx + 1) * 10000,
            id_final_piece      = (idx + 1) * 10000 + 99,
        )
        r = PRODUCT_RECIPES[ptype]
        multi = "MULTI-CELL" if r["mach_cells"] else "single-cell"
        print(f"\n  {ptype}  ({multi})  asm_tool=T{r['asm_tool']}  "
              f"cell=C{r['cell']//100}")
        for i, s in enumerate(slots):
            print(f"    [{i:2d}] {PHASE[i]:4s}  Cell={s['Cell']:3d}  "
                  f"Tool=T{s['Tool']}  {s['Tool_Time_Sec']:3d}s  "
                  f"Asm={'Y' if s['For_Assembly'] else 'N'}  "
                  f"Mat={s['Piece_Material']}  Type={s['Piece_Type']}")

    print("\n=== OPC-UA test (needs running CODESYS) ===")
    username = input("Username (blank=anonymous): ").strip() or None
    password = getpass.getpass("Password: ").strip() if username else None

    h = OpcUaHandler(username=username, password=password)
    try:
        h.connect()
        inv = h.read_warehouse_inventory()
        print(f"W1: total={inv['W1']}  wood={inv['W1_wood']}  "
              f"metal={inv['W1_metal']}")
        print(f"W2: total={inv['W2']}  finished={inv['W2_finished']}")
        print(f"Errors: {h.read_errors()}")

        ptype = input(f"\nType to dispatch {TYPES} (blank=skip): ").strip().upper()
        if ptype in PRODUCT_RECIPES:
            slots = build_recipe(ptype, id_recipe=99, id_procedure_start=9901,
                                 id_piece_start=99001, id_final_piece=99099)
            ok = h.dispatch(slots)
            print(f"dispatch -> {'PASS' if ok else 'FAIL'}")
            print(f"MES_Success : {h.read_success()}")
            print(f"Procedures  : {h.read_procedures()}")
            print(f"Errors      : {h.read_errors()}")

    except KeyboardInterrupt:
        pass
    finally:
        h.disconnect()