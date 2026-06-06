"""
plc_interface.py
================
High-level PLC interface.  The MES imports only this module, never
opcua_handler directly.

Public API
----------
Instance methods (need connect() first):
    plc.connect()                              -> bool
    plc.disconnect()
    plc.is_ready()                             -> bool
    plc.get_warehouse_status()                 -> dict
    plc.get_errors()                           -> list[dict]
    plc.get_procedures()                       -> list[dict]
    plc.get_cell_availability(cell)            -> dict
    plc.get_status()                           -> dict
    plc.create_pieces(piece_type, qty)         -> bool
    plc.create_pieces_for_unload(type, qty)    -> bool
    plc.unload_order(piece_type, quantity)     -> bool

Static helpers (no connection needed):
    PLCInterface.estimate_time(piece_type, qty)       -> int  (seconds)
    PLCInterface.raw_materials_needed(piece_type, qty)-> dict
    PLCInterface.split_into_docks(quantity)           -> list[int]

Supported product types
-----------------------
  RWW  Wood Round Table          -- all wood,  assembly C1
  SWW  Wood Square Table         -- all wood,  assembly C2
  RWM  Wood Round Top+Metal Legs -- mixed,     assembly C3  (T9)
  SWM  Wood Square Top+Metal Legs-- mixed,     assembly C4  (T9)
  RMM  Metal Round Table         -- all metal, assembly C3
  SMM  Metal Square Table        -- all metal, assembly C4
"""

from opcua_handler import OpcUaHandler, build_recipe, ELocation

# Estimated end-to-end production times (seconds, single piece, no queue).
# Mixed recipes (RWM/SWM) can machine legs and top in parallel across cells.
ESTIMATED_TIME = {
    "RWW": 60,    # C1: legs 10+10s, round top 30s, assembly 30s
    "SWW": 50,    # C2: legs 10+10s, square top 20s, assembly 30s
    "RWM": 100,   # C3+C1: metal legs 30s (C3) || wood top 30s (C1), asm 30s
    "SWM": 90,    # C4+C2: metal legs 30s (C4) || wood top 20s (C2), asm 30s
    "RMM": 105,   # C3: legs 30+30s, round top 35s, assembly 30s
    "SMM": 95,    # C4: legs 30+30s, square top 25s, assembly 30s
}

# Raw materials consumed per piece (3 raw pieces each).
# RWM/SWM use 2 metal (legs) + 1 wood (top).
RAW_MATERIALS = {
    "RWW": {"Wood": 3, "Metal": 0},
    "SWW": {"Wood": 3, "Metal": 0},
    "RWM": {"Wood": 1, "Metal": 2},
    "SWM": {"Wood": 1, "Metal": 2},
    "RMM": {"Wood": 0, "Metal": 3},
    "SMM": {"Wood": 0, "Metal": 3},
}

VALID_TYPES = list(ESTIMATED_TIME.keys())

class Machine:
    def __init__(self, cell: str, number):

        self.cell = cell
        self.number = number
        self.tool_change = 0;
        self.processed_piece = 0
        self.total_time_per_tool = {1: 0, 2: 0, 3: 0}

    def tool_change(self):
        self.tool_change += 1
    def process_piece(self, time, tool):
        self.processed_piece += 1
        self.total_time_per_tool[tool] += time

    def get_tool_change(self):
        return self.tool_change
    def get_processed_piece(self):
        return self.processed_piece
    def get_total_time_per_tool(self):
        return self.total_time_per_tool

class PLCInterface:

    def __init__(self, server_url="opc.tcp://127.0.0.1:4840",
                 username=None, password=None):
        self._handler   = OpcUaHandler(server_url, username, password)
        self._connected = False

        # Session-unique ID counters -- incremented every _alloc_ids() call.
        self._next_recipe_id    = 1
        self._next_procedure_id = 101
        self._next_piece_id     = 1001

    # -- ID allocation --------------------------------------------------------

    def _alloc_ids(self):
        """
        Reserve IDs for one 13-slot recipe and return
        (id_recipe, id_proc_start, id_piece_start, id_final_piece).

        Pieces:  id_piece_start+0, +1, +2  (3 raw)  and  id_piece_start+3  (final)
        Procs:   id_proc_start+0 .. +12    (13 procedures)
        """
        id_recipe      = self._next_recipe_id
        id_proc_start  = self._next_procedure_id
        id_piece_start = self._next_piece_id
        id_final       = self._next_piece_id + 3

        self._next_recipe_id    += 1
        self._next_procedure_id += 13   # 13 slots per recipe
        self._next_piece_id     += 4    # 3 raw + 1 final

        return id_recipe, id_proc_start, id_piece_start, id_final

    # -- Connection -----------------------------------------------------------

    def connect(self) -> bool:
        try:
            self._handler.connect()
            self._connected = True
            return True
        except Exception as e:
            print(f"[PLCInterface] Connection failed: {e}")
            return False

    def disconnect(self):
        try:
            self._handler.disconnect()
        except Exception:
            pass
        self._connected = False

    # -- Status reads ---------------------------------------------------------

    def is_ready(self) -> bool:
        """True when MES_Success is set (no active error state)."""
        try:
            return self._handler.read_success()
        except Exception:
            return True   # assume ready if read fails

    def get_warehouse_status(self) -> dict:
        """
        Return warehouse inventory.

        W1_wood / W1_metal  = schedulable raw stock
        W2_finished         = completed tables waiting for pick-up
        """
        try:
            return self._handler.read_warehouse_inventory()
        except Exception:
            return {"W1": 0, "W2": 0,
                    "W1_wood": 0, "W1_metal": 0, "W2_finished": 0}

    def get_errors(self) -> list:
        try:
            return self._handler.read_errors()
        except Exception:
            return []

    def get_procedures(self) -> list:
        try:
            return self._handler.read_procedures()
        except Exception:
            return []

    def get_cell_availability(self, cell: str) -> dict:
        """
        Check workstation availability for cell in {"C1","C2","C3","C4"}.
        Returns dict with any_free, all_busy, working[], done[].
        Falls back to all-free on read error (conservative assumption).
        """
        try:
            return self._handler.read_cell_workstation_tracking(cell)
        except Exception:
            return {"any_free": True, "all_busy": False,
                    "working": [False, False, False],
                    "done":    [False, False, False]}

    def get_status(self) -> dict:
        return {
            "connected":  self._connected,
            "ready":      self.is_ready(),
            "warehouse":  self.get_warehouse_status(),
            "errors":     self.get_errors(),
            "procedures": self.get_procedures(),
        }
    def get_machine_statistics(self) -> list:
        """Machine statistics from MES_Machine_Statistics (all 12 machines)."""
        try:
            return self._handler.read_machine_statistics()
        except Exception:
            return []

    # -- Production -----------------------------------------------------------

    def create_pieces(self, piece_type: str, quantity: int = 1) -> bool:
        """
        Dispatch `quantity` recipes of `piece_type` to the PLC.
        Each recipe is sent and acknowledged individually.
        Returns True only if ALL pieces were accepted.
        """
        piece_type = piece_type.upper()
        if piece_type not in VALID_TYPES:
            raise ValueError(f"Invalid type '{piece_type}'. Valid: {VALID_TYPES}")

        results = []
        for n in range(quantity):
            id_recipe, id_proc, id_piece, id_final = self._alloc_ids()
            slots = build_recipe(
                piece_type         = piece_type,
                id_recipe          = id_recipe,
                id_procedure_start = id_proc,
                id_piece_start     = id_piece,
                id_final_piece     = id_final,
            )
            print(f"[PLCInterface] Piece {n+1}/{quantity} {piece_type} "
                  f"(recipe={id_recipe}, proc_start={id_proc})")
            ok = self._handler.dispatch(slots)
            results.append(ok)
            if not ok:
                print(f"[PLCInterface] PLC rejected piece {n+1}")

        return all(results)

    def create_pieces_for_unload(self, piece_type: str,
                                 quantity: int = 1) -> bool:
        """
        Dispatch `quantity` recipes routed to the unload station (U=40).
        Functionally identical to create_pieces() but semantically marks
        the order as destined for immediate unloading rather than storage.
        Max 6 pieces per call (one dock capacity).
        """
        piece_type = piece_type.upper()
        if piece_type not in VALID_TYPES:
            raise ValueError(f"Invalid type: {piece_type}")
        if quantity > 6:
            raise ValueError(
                "Max 6 per call -- use unload_order() for larger quantities")

        results = []
        for n in range(quantity):
            id_recipe, id_proc, id_piece, id_final = self._alloc_ids()
            slots = build_recipe(
                piece_type         = piece_type,
                id_recipe          = id_recipe,
                id_procedure_start = id_proc,
                id_piece_start     = id_piece,
                id_final_piece     = id_final,
                unload_location    = ELocation.U,
            )
            print(f"[PLCInterface] Unload piece {n+1}/{quantity} {piece_type}")
            ok = self._handler.dispatch(slots)
            results.append(ok)
            if not ok:
                print(f"[PLCInterface] PLC rejected unload piece {n+1}")

        return all(results)

    def unload_order(self, piece_type: str, quantity: int) -> bool:
        """
        Produce and unload a full order, splitting into batches of max 6.
        Returns True only if all batches are accepted.
        """
        batches = self.split_into_docks(quantity)
        if len(batches) > 5:
            raise ValueError(
                f"Order of {quantity} requires {len(batches)} batches; max 5")
        results = []
        for i, qty in enumerate(batches):
            print(f"[PLCInterface] Batch {i+1}/{len(batches)}: {qty}x {piece_type}")
            ok = self.create_pieces_for_unload(piece_type, qty)
            results.append(ok)
        return all(results)

    # -- Static helpers -------------------------------------------------------

    @staticmethod
    def estimate_time(piece_type: str, quantity: int) -> int:
        """Rough estimate of total production time in seconds."""
        pt = piece_type.upper()
        if pt not in ESTIMATED_TIME:
            raise ValueError(f"Unknown type: {pt}")
        return ESTIMATED_TIME[pt] * quantity

    @staticmethod
    def raw_materials_needed(piece_type: str, quantity: int) -> dict:
        """Returns {"Wood": n, "Metal": n} for the order."""
        pt = piece_type.upper()
        if pt not in RAW_MATERIALS:
            raise ValueError(f"Unknown type: {pt}")
        return {k: v * quantity for k, v in RAW_MATERIALS[pt].items()}

    @staticmethod
    def split_into_docks(quantity: int) -> list:
        """Split quantity into batches of max 6 (one dock's capacity)."""
        docks, remaining = [], quantity
        while remaining > 0:
            batch = min(6, remaining)
            docks.append(batch)
            remaining -= batch
        return docks


# -- Self-test ----------------------------------------------------------------

if __name__ == "__main__":
    print("=== Static helpers ===")
    for ptype in VALID_TYPES:
        est  = PLCInterface.estimate_time(ptype, 1)
        mats = PLCInterface.raw_materials_needed(ptype, 1)
        print(f"  {ptype}: {est}s  {mats}")
    print(f"\n  split_into_docks(14) = {PLCInterface.split_into_docks(14)}")

    print("\n=== OPC-UA test (needs running CODESYS) ===")
    plc = PLCInterface()
    if plc.connect():
        print(f"Status: {plc.get_status()}")
        ptype = input(f"Type to produce {VALID_TYPES} (blank=skip): ").strip().upper()
        if ptype in VALID_TYPES:
            ok = plc.create_pieces(ptype, 1)
            print(f"Result: {'PASS' if ok else 'FAIL'}")
        plc.disconnect()
    else:
        print("Could not connect.")
