"""
opcua_handler.py
================
Low-level OPC-UA handler for CODESYS.

RECIPE STRUCTURE (confirmed from Final_Test PLC code):
======================================================
Each piece requires EXACTLY 14 slots (indices 0-13) with strictly
incrementing ID_Procedure values.

Slot layout:
  [0]  Load piece 1    Cell=L,  Tool=IDLE, For_Assembly=FALSE
  [1]  Load piece 2    Cell=L,  Tool=IDLE, For_Assembly=FALSE
  [2]  Load piece 3    Cell=L,  Tool=IDLE, For_Assembly=FALSE
  [3]  Machine piece 1 Cell=C?, Tool=T?,   Tool_Time_Sec=?
  [4]  Machine piece 2 Cell=C?, Tool=T?,   Tool_Time_Sec=?
  [5]  Machine piece 3 Cell=C?, Tool=T?,   Tool_Time_Sec=?
  [6]  Transport p1    Cell=T,  Tool=IDLE, For_Assembly=TRUE
  [7]  Transport p2    Cell=T,  Tool=IDLE, For_Assembly=TRUE
  [8]  Transport p3    Cell=T,  Tool=IDLE, For_Assembly=TRUE
  [9]  Assembly Leg1   Cell=C?, Tool=T8/T9, IDs_Assembly.Leg_1=ID
  [10] Assembly Leg2   Cell=C?, Tool=T8/T9, IDs_Assembly.Leg_2=ID
  [11] Assembly Top    Cell=C?, Tool=T8/T9, IDs_Assembly.Top=ID
  [12] (slot 10 in test was skipped — 11 was Leg2, 12 was Top)
  [13] Unload          Cell=U,  Tool=IDLE, For_Assembly=FALSE

NOTE: Final_Test uses slots 9, 11, 12 for assembly (skips 10).
      We use 9, 10, 11 and 12 for unload (14 slots total = indices 0-13).

Confirmed enum values from PLC XML:
  E_Location : L=30, T=20, U=40, W1=50, W2=60, C1=100, C2=200, C3=300, C4=400
  E_Tool     : IDLE=0, T1=1, T2=2, T3=3, T4=4, T5=5, T6=6, T8=8, T9=9, T10=10, T11=11
  E_Material : WOOD=1, METAL=2
  E_PieceType: IDLE=0, WOOD_ROUND_TOP=3, WOOD_SQUARE_TOP=4, WOOD_LEG=5,
               METAL_ROUND_TOP=6, METAL_SQUARE_TOP=7, METAL_LEG=8,
               WOOD_ROUND_TABLE=9, WOOD_SQUARE_TABLE=10,
               WOOD_ROUND_TOP_METAL_LEGS=11, WOOD_SQUARE_TOP_METAL_LEGS=12,
               METAL_ROUND_TABLE=13, METAL_SQUARE_TABLE=14
  E_Procedure_Status: IDLE=0, NEW_ORDER=1, EXECUTION=2, STOPPED=3, COMPLETED=4

MAX_LOGIC_N_PROCEDURES: set to 150 (from Final_Test PLC code).
"""

from opcua import Client, ua
import time
import logging

logger = logging.getLogger(__name__)

SERVER_URL           = "opc.tcp://127.0.0.1:4840"
GVL_BASE             = "ns=4;s=|var|CODESYS Control Win V3 x64.Application.GVL."
RECIPE_POLL_INTERVAL = 0.2
RECIPE_ACK_TIMEOUT   = 15.0


# ── Enum mirrors (confirmed from PLC XML) ─────────────────────────────────────

class ELocation:
    IDLE = 0
    L    = 30
    T    = 20
    U    = 40
    W1   = 50
    W2   = 60
    C1   = 100
    C2   = 200
    C3   = 300
    C4   = 400

class ETool:
    IDLE = 0
    T1   = 1
    T2   = 2
    T3   = 3
    T4   = 4
    T5   = 5
    T6   = 6
    T8   = 8
    T9   = 9
    T10  = 10
    T11  = 11

class EMaterial:
    WOOD  = 1
    METAL = 2

class EPieceType:
    IDLE                     = 0
    WOOD_ROUND_TOP           = 3
    WOOD_SQUARE_TOP          = 4
    WOOD_LEG                 = 5
    METAL_ROUND_TOP          = 6
    METAL_SQUARE_TOP         = 7
    METAL_LEG                = 8
    WOOD_ROUND_TABLE         = 9   # RWW
    WOOD_SQUARE_TABLE        = 10  # SWW
    WOOD_ROUND_TOP_METAL_LEGS= 11  # RWM
    WOOD_SQUARE_TOP_METAL_LEGS=12  # SWM
    METAL_ROUND_TABLE        = 13  # RMM
    METAL_SQUARE_TABLE       = 14  # SMM

class EProcedureStatus:
    IDLE      = 0
    NEW_ORDER = 1
    EXECUTION = 2
    STOPPED   = 3
    COMPLETED = 4


# ── Product recipe definitions ────────────────────────────────────────────────
# Based on Final_Test PLC code and spec Table 3

PRODUCT_RECIPES = {
    "RWW": {
        "cell":         ELocation.C1,
        "materials":    [EMaterial.WOOD,  EMaterial.WOOD,  EMaterial.WOOD],
        "raw_types":    [EPieceType.WOOD_LEG, EPieceType.WOOD_LEG, EPieceType.WOOD_ROUND_TOP],
        "mach_tools":   [ETool.T3, ETool.T3, ETool.T1],
        "mach_times":   [10, 10, 30],
        "mach_types":   [EPieceType.WOOD_LEG, EPieceType.WOOD_LEG, EPieceType.WOOD_ROUND_TOP],
        "asm_tool":     ETool.T8,
        "asm_time":     10,
        "final_type":   EPieceType.WOOD_ROUND_TABLE,
        "asm_leg1_idx": 0,   # piece[0] = Leg1
        "asm_leg2_idx": 1,   # piece[1] = Leg2
        "asm_top_idx":  2,   # piece[2] = Top
    },
    "SWW": {
        "cell":         ELocation.C2,
        "materials":    [EMaterial.WOOD,  EMaterial.WOOD,  EMaterial.WOOD],
        "raw_types":    [EPieceType.WOOD_LEG, EPieceType.WOOD_LEG, EPieceType.WOOD_SQUARE_TOP],
        "mach_tools":   [ETool.T3, ETool.T3, ETool.T2],
        "mach_times":   [10, 10, 20],
        "mach_types":   [EPieceType.WOOD_LEG, EPieceType.WOOD_LEG, EPieceType.WOOD_SQUARE_TOP],
        "asm_tool":     ETool.T8,
        "asm_time":     10,
        "final_type":   EPieceType.WOOD_SQUARE_TABLE,
        "asm_leg1_idx": 0,
        "asm_leg2_idx": 1,
        "asm_top_idx":  2,
    },
    "RWM": {
        "cell":         ELocation.C1,
        "materials":    [EMaterial.METAL, EMaterial.METAL, EMaterial.WOOD],
        "raw_types":    [EPieceType.METAL_LEG, EPieceType.METAL_LEG, EPieceType.WOOD_ROUND_TOP],
        "mach_tools":   [ETool.T5, ETool.T5, ETool.T1],
        "mach_times":   [30, 30, 30],
        "mach_types":   [EPieceType.METAL_LEG, EPieceType.METAL_LEG, EPieceType.WOOD_ROUND_TOP],
        "asm_tool":     ETool.T9,
        "asm_time":     10,
        "final_type":   EPieceType.WOOD_ROUND_TOP_METAL_LEGS,
        "asm_leg1_idx": 0,
        "asm_leg2_idx": 1,
        "asm_top_idx":  2,
    },
    "SWM": {
        "cell":         ELocation.C2,
        "materials":    [EMaterial.METAL, EMaterial.METAL, EMaterial.WOOD],
        "raw_types":    [EPieceType.METAL_LEG, EPieceType.METAL_LEG, EPieceType.WOOD_SQUARE_TOP],
        "mach_tools":   [ETool.T5, ETool.T5, ETool.T2],
        "mach_times":   [30, 30, 20],
        "mach_types":   [EPieceType.METAL_LEG, EPieceType.METAL_LEG, EPieceType.WOOD_SQUARE_TOP],
        "asm_tool":     ETool.T9,
        "asm_time":     10,
        "final_type":   EPieceType.WOOD_SQUARE_TOP_METAL_LEGS,
        "asm_leg1_idx": 0,
        "asm_leg2_idx": 1,
        "asm_top_idx":  2,
    },
    "RMM": {
        "cell":         ELocation.C3,
        "materials":    [EMaterial.METAL, EMaterial.METAL, EMaterial.METAL],
        "raw_types":    [EPieceType.METAL_LEG, EPieceType.METAL_LEG, EPieceType.METAL_ROUND_TOP],
        "mach_tools":   [ETool.T5, ETool.T5, ETool.T4],
        "mach_times":   [30, 30, 35],
        "mach_types":   [EPieceType.METAL_LEG, EPieceType.METAL_LEG, EPieceType.METAL_ROUND_TOP],
        "asm_tool":     ETool.T8,
        "asm_time":     10,
        "final_type":   EPieceType.METAL_ROUND_TABLE,
        "asm_leg1_idx": 0,
        "asm_leg2_idx": 1,
        "asm_top_idx":  2,
    },
    "SMM": {
        "cell":         ELocation.C3,
        "materials":    [EMaterial.METAL, EMaterial.METAL, EMaterial.METAL],
        "raw_types":    [EPieceType.METAL_LEG, EPieceType.METAL_LEG, EPieceType.METAL_SQUARE_TOP],
        "mach_tools":   [ETool.T5, ETool.T5, ETool.T6],
        "mach_times":   [30, 30, 25],
        "mach_types":   [EPieceType.METAL_LEG, EPieceType.METAL_LEG, EPieceType.METAL_SQUARE_TOP],
        "asm_tool":     ETool.T8,
        "asm_time":     10,
        "final_type":   EPieceType.METAL_SQUARE_TABLE,
        "asm_leg1_idx": 0,
        "asm_leg2_idx": 1,
        "asm_top_idx":  2,
    },
}


# ── Recipe builder ────────────────────────────────────────────────────────────

def build_recipe(
    piece_type:         str,
    id_recipe:          int,
    id_procedure_start: int,
    id_piece_start:     int,
    id_final_piece:     int,
    unload_location:    int = ELocation.U,
) -> list:
    """
    Build the 14-slot recipe for one piece.

    Slot indices 0-13:
      0-2:  Loading (Cell=L)
      3-5:  Machining (Cell=C?)
      6-8:  Transport (Cell=T)
      9-11: Assembly (Cell=C?, Leg1/Leg2/Top)
      12:   (unused — kept for alignment, ID_Procedure increments)
      13:   Unload (Cell=U)

    NOTE: We send 13 meaningful slots but ID_Procedure spans 0-13
    to match Final_Test's numbering (101-113).
    Actually Final_Test uses indices 0,1,2,3,4,5,6,7,8,9,11,12,13
    skipping index 10. We use 0-12 (13 slots) with index 13 = unload.
    """
    r    = PRODUCT_RECIPES[piece_type.upper()]
    cell = r["cell"]
    slots = []
    pid   = [id_piece_start, id_piece_start + 1, id_piece_start + 2]

    # ── Loading (slots 0-2) ───────────────────────────────────────────────────
    for k in range(3):
        slots.append({
            "N_Slot":                    len(slots),
            "ID_Procedure":              id_procedure_start + len(slots),
            "ID_Recipe":                 id_recipe,
            "ID_Piece":                  pid[k],
            "Status":                    EProcedureStatus.NEW_ORDER,
            "Cell":                      ELocation.L,
            "Tool":                      ETool.IDLE,
            "Tool_Time_Sec":             0,
            "Piece_Material":            r["materials"][k],
            "Piece_Type":                r["raw_types"][k],
            "For_Assembly":              False,
            "ID_Assembly_Final_Product": 0,
            "IDs_Assembly_Leg_1":        0,
            "IDs_Assembly_Leg_2":        0,
            "IDs_Assembly_Top":          0,
        })

    # ── Machining (slots 3-5) ─────────────────────────────────────────────────
    for k in range(3):
        slots.append({
            "N_Slot":                    len(slots),
            "ID_Procedure":              id_procedure_start + len(slots),
            "ID_Recipe":                 id_recipe,
            "ID_Piece":                  pid[k],
            "Status":                    EProcedureStatus.NEW_ORDER,
            "Cell":                      cell,
            "Tool":                      r["mach_tools"][k],
            "Tool_Time_Sec":             r["mach_times"][k],
            "Piece_Material":            r["materials"][k],
            "Piece_Type":                r["mach_types"][k],
            "For_Assembly":              False,
            "ID_Assembly_Final_Product": 0,
            "IDs_Assembly_Leg_1":        0,
            "IDs_Assembly_Leg_2":        0,
            "IDs_Assembly_Top":          0,
        })

    # ── Transport (slots 6-8) ─────────────────────────────────────────────────
    for k in range(3):
        slots.append({
            "N_Slot":                    len(slots),
            "ID_Procedure":              id_procedure_start + len(slots),
            "ID_Recipe":                 id_recipe,
            "ID_Piece":                  pid[k],
            "Status":                    EProcedureStatus.NEW_ORDER,
            "Cell":                      ELocation.T,
            "Tool":                      ETool.IDLE,
            "Tool_Time_Sec":             0,
            "Piece_Material":            r["materials"][k],
            "Piece_Type":                r["mach_types"][k],
            "For_Assembly":              True,
            "ID_Assembly_Final_Product": 0,
            "IDs_Assembly_Leg_1":        0,
            "IDs_Assembly_Leg_2":        0,
            "IDs_Assembly_Top":          0,
        })

    # ── Assembly Leg 1 (slot 9) ───────────────────────────────────────────────
    leg1_idx = r["asm_leg1_idx"]
    slots.append({
        "N_Slot":                    len(slots),
        "ID_Procedure":              id_procedure_start + len(slots),
        "ID_Recipe":                 id_recipe,
        "ID_Piece":                  pid[leg1_idx],
        "Status":                    EProcedureStatus.NEW_ORDER,
        "Cell":                      cell,
        "Tool":                      r["asm_tool"],
        "Tool_Time_Sec":             r["asm_time"],
        "Piece_Material":            r["materials"][leg1_idx],
        "Piece_Type":                r["final_type"],
        "For_Assembly":              True,
        "ID_Assembly_Final_Product": id_final_piece,
        "IDs_Assembly_Leg_1":        pid[leg1_idx],
        "IDs_Assembly_Leg_2":        0,
        "IDs_Assembly_Top":          0,
    })

    # ── Assembly Leg 2 (slot 10) ──────────────────────────────────────────────
    leg2_idx = r["asm_leg2_idx"]
    slots.append({
        "N_Slot":                    len(slots),
        "ID_Procedure":              id_procedure_start + len(slots),
        "ID_Recipe":                 id_recipe,
        "ID_Piece":                  pid[leg2_idx],
        "Status":                    EProcedureStatus.NEW_ORDER,
        "Cell":                      cell,
        "Tool":                      r["asm_tool"],
        "Tool_Time_Sec":             r["asm_time"],
        "Piece_Material":            r["materials"][leg2_idx],
        "Piece_Type":                r["final_type"],
        "For_Assembly":              True,
        "ID_Assembly_Final_Product": id_final_piece,
        "IDs_Assembly_Leg_1":        0,
        "IDs_Assembly_Leg_2":        pid[leg2_idx],
        "IDs_Assembly_Top":          0,
    })

    # ── Assembly Top (slot 11) ────────────────────────────────────────────────
    top_idx = r["asm_top_idx"]
    slots.append({
        "N_Slot":                    len(slots),
        "ID_Procedure":              id_procedure_start + len(slots),
        "ID_Recipe":                 id_recipe,
        "ID_Piece":                  pid[top_idx],
        "Status":                    EProcedureStatus.NEW_ORDER,
        "Cell":                      cell,
        "Tool":                      r["asm_tool"],
        "Tool_Time_Sec":             r["asm_time"],
        "Piece_Material":            r["materials"][top_idx],
        "Piece_Type":                r["final_type"],
        "For_Assembly":              True,
        "ID_Assembly_Final_Product": id_final_piece,
        "IDs_Assembly_Leg_1":        0,
        "IDs_Assembly_Leg_2":        0,
        "IDs_Assembly_Top":          pid[top_idx],
    })

    # ── Unload (slot 12) ──────────────────────────────────────────────────────
    slots.append({
        "N_Slot":                    len(slots),
        "ID_Procedure":              id_procedure_start + len(slots),
        "ID_Recipe":                 id_recipe,
        "ID_Piece":                  id_final_piece,
        "Status":                    EProcedureStatus.NEW_ORDER,
        "Cell":                      unload_location,
        "Tool":                      ETool.IDLE,
        "Tool_Time_Sec":             0,
        "Piece_Material":            r["materials"][0],
        "Piece_Type":                r["final_type"],
        "For_Assembly":              False,
        "ID_Assembly_Final_Product": 0,
        "IDs_Assembly_Leg_1":        0,
        "IDs_Assembly_Leg_2":        0,
        "IDs_Assembly_Top":          0,
    })

    assert len(slots) == 13, f"Expected 13 slots, got {len(slots)}"
    return slots


# ── OpcUaHandler ──────────────────────────────────────────────────────────────

class OpcUaHandler:
    """Low-level CODESYS OPC-UA handler. Only plc_interface.py imports this."""

    def __init__(self, server_url=SERVER_URL, username=None, password=None):
        self.server_url = server_url
        self.username   = username
        self.password   = password
        self.client     = Client(server_url)
        self.connected  = False

    # ── Connection ────────────────────────────────────────────────────────────

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

    # ── Low-level helpers ─────────────────────────────────────────────────────

    def _node(self, path):
        self._ensure_connected()
        return self.client.get_node(GVL_BASE + path)

    def _read(self, path):
        return self._node(path).get_value()

    def _write(self, path, value):
        """Auto-typed write."""
        try:
            node = self._node(path)
            t    = node.get_data_type_as_variant_type()
            node.set_value(ua.Variant(value, t))
            return True
        except Exception as e:
            logger.error(f"[OpcUaHandler] Write failed '{path}': {e}")
            return False

    # ── Procedure limits ──────────────────────────────────────────────────────

    def write_procedure_limits(self, value=150):
        """
        Set MAX_LOGIC_N_PROCEDURES fields.
        Final_Test uses 150 for all fields.
        """
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
        ok = True
        for path in fields:
            if not self._write(path, value):
                ok = False
        if ok:
            print(f"[OpcUaHandler] Procedure limits set to {value}")
        return ok

    # ── Recipe write ──────────────────────────────────────────────────────────

    def write_slot(self, slot_index, slot):
        """Write one ST_Procedure slot."""
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
        for path, value in fields:
            if not self._write(path, value):
                ok = False
        return ok

    def write_recipe(self, slots):
        """Write all slots to MES_Recipe."""
        ok = True
        for i, slot in enumerate(slots):
            if self.write_slot(i, slot):
                print(f"[OpcUaHandler] Slot[{i:2d}] OK "
                      f"Cell={slot['Cell']} Tool={slot['Tool']} "
                      f"ID_Proc={slot['ID_Procedure']}")
            else:
                print(f"[OpcUaHandler] Slot[{i:2d}] FAILED")
                ok = False
        return ok

    # ── Trigger ───────────────────────────────────────────────────────────────

    def trigger_recipe(self, timeout=RECIPE_ACK_TIMEOUT):
        """Set MES_Read_Recipes=TRUE, poll until PLC resets it."""
        self._write("MES_Read_Recipes", True)
        print("[OpcUaHandler] Triggered, waiting for PLC ack...")
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if not bool(self._read("MES_Read_Recipes")):
                    print("[OpcUaHandler] PLC acknowledged.")
                    return True
            except Exception:
                pass
            time.sleep(RECIPE_POLL_INTERVAL)
        print(f"[OpcUaHandler] No ack within {timeout}s, forcing reset.")
        self._write("MES_Read_Recipes", False)
        return False

    def dispatch(self, slots, limits=150):
        """
        Full sequence: write limits → write slots → trigger.
        Returns True if PLC acknowledged.
        """
        if not slots:
            return False
        self.write_procedure_limits(limits)
        if not self.write_recipe(slots):
            print("[OpcUaHandler] Recipe write failed, not triggering.")
            return False
        return self.trigger_recipe()

    # ── MES reads ─────────────────────────────────────────────────────────────

    def read_success(self):
        return bool(self._read("MES_Success"))

    def read_num_errors(self):
        return int(self._read("MES_Num_Errors"))

    def read_warehouse_inventory(self):
        w1 = int(self._read("MES_Warehouse_Inventory.N_Pieces_W1"))
        w2 = int(self._read("MES_Warehouse_Inventory.N_Pieces_W2"))
        return {"W1": w1, "W2": w2}

    def read_procedures(self, max_slots=10):
        """
        Read active MES_Procedures — excludes IDLE (0) and COMPLETED (4).
        Returns only procedures genuinely in progress.

        E_Procedure_Status: IDLE=0, NEW_ORDER=1, EXECUTION=2, STOPPED=3, COMPLETED=4
        """
        results = []
        for i in range(max_slots):
            try:
                status  = int(self._read(f"MES_Procedures[{i}].Status"))
                id_proc = int(self._read(f"MES_Procedures[{i}].ID_Procedure"))
                aborted = bool(self._read(f"MES_Procedures[{i}].Successfully_Abort"))

                # Stop at empty slot
                if id_proc == 0:
                    break

                # Only include active procedures (not IDLE=0 or COMPLETED=4)
                if status not in (0, 4):
                    results.append({
                        "status":  status,
                        "id":      id_proc,
                        "aborted": aborted,
                    })
            except Exception:
                break
        return results

    def read_errors(self):
        n = self.read_num_errors()
        errors = []
        for i in range(min(n, 20)):
            try:
                code    = int(self._read(f"MES_Errors[{i}].Error_Code"))
                slot    = int(self._read(f"MES_Errors[{i}].Slot_Index"))
                id_proc = int(self._read(f"MES_Errors[{i}].ID_Procedure"))
                errors.append({"code": code, "slot": slot, "procedure_id": id_proc})
            except Exception:
                break
        return errors


# ── Self-test ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import getpass

    print("=== Recipe builder test (no PLC needed) ===")
    slots = build_recipe(
        piece_type="RWW",
        id_recipe=10,
        id_procedure_start=101,
        id_piece_start=60001,
        id_final_piece=60100,
    )
    print(f"Built {len(slots)} slots for RWW:")
    for i, s in enumerate(slots):
        print(f"  [{i:2d}] ID_Proc={s['ID_Procedure']:4d} "
              f"Cell={s['Cell']:4d} Tool={s['Tool']:2d} "
              f"Time={s['Tool_Time_Sec']:3d}s "
              f"Asm={str(s['For_Assembly']):5s} "
              f"Type={s['Piece_Type']:2d}")

    print("\n=== OPC-UA test ===")
    username = input("Username (blank=anonymous): ").strip() or None
    password = getpass.getpass("Password: ").strip() if username else None

    h = OpcUaHandler(username=username, password=password)
    try:
        h.connect()
        print(f"Warehouse: {h.read_warehouse_inventory()}")
        print(f"Errors   : {h.read_errors()}")

        ok = h.dispatch(slots)
        print(f"\nResult: {'PASS' if ok else 'FAIL'}")
        print(f"MES_Success: {h.read_success()}")
        print(f"Errors     : {h.read_errors()}")
        print(f"Procedures : {h.read_procedures()}")

    except KeyboardInterrupt:
        pass
    finally:
        h.disconnect()