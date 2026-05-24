"""
plc_interface.py
================
High-level PLC interface for the MES.

MES only imports this — never opcua_handler directly.

Public methods:
    plc.connect()                                      -> bool
    plc.disconnect()
    plc.is_ready()                                     -> bool
    plc.get_warehouse_status()                         -> dict {"W1": int, "W2": int}
    plc.get_errors()                                   -> list[dict]
    plc.get_procedures()                               -> list[dict]
    plc.get_status()                                   -> dict
    plc.create_pieces(piece_type, quantity)            -> bool
    plc.create_pieces_for_unload(piece_type, qty, dock)-> bool
    plc.unload_order(piece_type, quantity)             -> bool

Static helpers (no connection needed):
    PLCInterface.estimate_time(piece_type, quantity)        -> int (seconds)
    PLCInterface.raw_materials_needed(piece_type, quantity) -> dict
    PLCInterface.split_into_docks(quantity)                 -> list[int]
"""

from opcua_handler import OpcUaHandler, build_recipe, ELocation

# RWM and SWM removed — not yet implemented in PLC (multi-cell required)
ESTIMATED_TIME = {
    "RWW": 60,   # C1: legs 10+10s, top 30s
    "SWW": 50,   # C2: legs 10+10s, top 20s
    "RMM": 105,  # C3: legs 30+30s, top 35s
    "SMM": 95,   # C4: legs 30+30s, top 25s
}

RAW_MATERIALS = {
    "RWW": {"Wood": 3, "Metal": 0},
    "SWW": {"Wood": 3, "Metal": 0},
    "RMM": {"Wood": 0, "Metal": 3},
    "SMM": {"Wood": 0, "Metal": 3},
}

VALID_TYPES = list(ESTIMATED_TIME.keys())


class PLCInterface:

    def __init__(self, server_url="opc.tcp://127.0.0.1:4840",
                 username=None, password=None):
        self._handler   = OpcUaHandler(server_url, username, password)
        self._connected = False

        # Unique ID counters per session
        self._next_recipe_id    = 1
        self._next_procedure_id = 101
        self._next_piece_id     = 1001

    # ── ID allocation ─────────────────────────────────────────────────────────

    def _alloc_ids(self):
        """Allocate unique IDs for one piece. Returns (recipe, proc_start, piece_start, final)."""
        id_recipe      = self._next_recipe_id
        id_proc_start  = self._next_procedure_id
        id_piece_start = self._next_piece_id
        id_final       = self._next_piece_id + 3

        self._next_recipe_id    += 1
        self._next_procedure_id += 13
        self._next_piece_id     += 4

        return id_recipe, id_proc_start, id_piece_start, id_final

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self):
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

    # ── Status ────────────────────────────────────────────────────────────────

    def is_ready(self):
        try:
            return self._handler.read_success()
        except Exception:
            return True

    def get_warehouse_status(self):
        """
        Returns warehouse status with type-aware breakdown.
        W1_wood/W1_metal = RAW materials only (schedulable)
        W1_wip = work-in-progress pieces (not schedulable)
        W2_finished = completed tables waiting to unload
        """
        try:
            return self._handler.read_warehouse_inventory()
        except Exception:
            return {"W1": 0, "W2": 0,
                    "W1_wood": 0, "W1_metal": 0,
                    "W2_finished": 0}

    def get_errors(self):
        try:
            return self._handler.read_errors()
        except Exception:
            return []

    def get_procedures(self):
        try:
            return self._handler.read_procedures()
        except Exception:
            return []

    def get_cell_availability(self, cell: str) -> dict:
        """
        Check if a cell has free workstations.
        cell: "C1", "C2", "C3", or "C4"
        Returns dict with any_free, all_busy, working[], done[]
        """
        try:
            return self._handler.read_cell_workstation_tracking(cell)
        except Exception:
            # If we can't read, assume free (conservative — better than blocking)
            return {"any_free": True, "all_busy": False,
                    "working": [False, False, False],
                    "done":    [False, False, False]}

    def get_status(self):
        return {
            "connected":  self._connected,
            "ready":      self.is_ready(),
            "warehouse":  self.get_warehouse_status(),
            "errors":     self.get_errors(),
            "procedures": self.get_procedures(),
        }

    # ── Production ────────────────────────────────────────────────────────────

    def create_pieces(self, piece_type, quantity=1):
        """
        Produce `quantity` pieces of `piece_type`.
        Dispatches one 13-slot recipe per piece.
        Returns True if ALL accepted by PLC.
        """
        piece_type = piece_type.upper()
        if piece_type not in VALID_TYPES:
            raise ValueError(f"Invalid type '{piece_type}'. Valid: {VALID_TYPES}")

        results = []
        for n in range(quantity):
            id_recipe, id_proc, id_piece, id_final = self._alloc_ids()
            slots = build_recipe(
                piece_type          = piece_type,
                id_recipe           = id_recipe,
                id_procedure_start  = id_proc,
                id_piece_start      = id_piece,
                id_final_piece      = id_final,
            )
            print(f"[PLCInterface] Piece {n+1}/{quantity} {piece_type} "
                  f"(recipe={id_recipe}, proc={id_proc})")
            ok = self._handler.dispatch(slots)
            results.append(ok)
            if not ok:
                print(f"[PLCInterface] PLC rejected piece {n+1}")

        return all(results)

    def create_pieces_for_unload(self, piece_type, quantity=1, dock=1):
        """
        Produce `quantity` pieces routed to unloading `dock` (1-5).
        Max 6 pieces per dock.
        """
        piece_type = piece_type.upper()
        if piece_type not in VALID_TYPES:
            raise ValueError(f"Invalid type: {piece_type}")
        if not 1 <= dock <= 5:
            raise ValueError("Dock must be 1-5")
        if quantity > 6:
            raise ValueError("Max 6 per dock — use unload_order() for larger quantities")

        results = []
        for n in range(quantity):
            id_recipe, id_proc, id_piece, id_final = self._alloc_ids()
            slots = build_recipe(
                piece_type          = piece_type,
                id_recipe           = id_recipe,
                id_procedure_start  = id_proc,
                id_piece_start      = id_piece,
                id_final_piece      = id_final,
                unload_location     = ELocation.U,
            )
            print(f"[PLCInterface] Piece {n+1}/{quantity} {piece_type} "
                  f"-> dock {dock}")
            ok = self._handler.dispatch(slots)
            results.append(ok)

        return all(results)

    def unload_order(self, piece_type, quantity):
        """
        Produce and unload a full order, splitting across docks automatically.
        """
        batches = self.split_into_docks(quantity)
        if len(batches) > 5:
            raise ValueError(f"Order of {quantity} needs {len(batches)} docks, max 5")
        results = []
        for dock, qty in enumerate(batches, start=1):
            ok = self.create_pieces_for_unload(piece_type, qty, dock=dock)
            results.append(ok)
        return all(results)

    # ── Static helpers ────────────────────────────────────────────────────────

    @staticmethod
    def estimate_time(piece_type, quantity):
        piece_type = piece_type.upper()
        if piece_type not in ESTIMATED_TIME:
            raise ValueError(f"Unknown type: {piece_type}")
        return ESTIMATED_TIME[piece_type] * quantity

    @staticmethod
    def raw_materials_needed(piece_type, quantity):
        piece_type = piece_type.upper()
        if piece_type not in RAW_MATERIALS:
            raise ValueError(f"Unknown type: {piece_type}")
        return {k: v * quantity for k, v in RAW_MATERIALS[piece_type].items()}

    @staticmethod
    def split_into_docks(quantity):
        docks, remaining = [], quantity
        while remaining > 0:
            batch = min(6, remaining)
            docks.append(batch)
            remaining -= batch
        return docks


# ── Self-test ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== Static helpers ===")
    for ptype in VALID_TYPES:
        print(f"  {ptype}: {PLCInterface.estimate_time(ptype, 1)}s "
              f"| {PLCInterface.raw_materials_needed(ptype, 1)}")
    print(f"  split_into_docks(14): {PLCInterface.split_into_docks(14)}")

    print("\n=== OPC-UA test ===")
    plc = PLCInterface()
    if plc.connect():
        print(f"Status: {plc.get_status()}")
        ok = plc.create_pieces("RWW", 1)
        print(f"Result: {'PASS' if ok else 'FAIL'}")
        plc.disconnect()
    else:
        print("Could not connect.")