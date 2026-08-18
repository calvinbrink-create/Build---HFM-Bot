"""Pattern memory - recognition library for live chart setup construction.

Spec #52 loss pattern brain, #53 trap library, #46 similarity search,
#137/#138 skipped-winner/loser learning.

RUNTIME CONTRACT
Three independent tests this session showed chart shape CANNOT forecast
direction:
    unconditional direction   0 of 85 configurations survived Bonferroni
    shorts landed exactly on their base rate
    letting the chart choose the side  ->  -419R, 0 of 4 folds
The active asset engine determines direction from the live chart event. This
module recognizes similar historical shapes and returns observed outcomes for
both directions. It never chooses a direction and it never vetoes execution.

The query requires 48 bars of context and returns the nearest historical
observations without changing the chart direction.
"""
from __future__ import annotations

import json
import math
import os
import threading
from pathlib import Path

try:
    import numpy as np
except ImportError:                                    # pragma: no cover
    np = None

DIMS = 12
LENGTH = 48
DEFAULT_PATH = os.getenv(
    "MT5_PATTERN_MEMORY_PATH", "/opt/cipherfx_mt5/config/pattern_memory.npz"
)


def encode_shape(closes, length: int = LENGTH, dims: int = DIMS):
    """Scale-free chart shape: resample -> min/max normalise -> centre -> unit norm.

    Returns a list of floats (dims long) or None when there is not enough data.
    Cosine similarity between two of these measures SHAPE alone, independent of
    price level or amplitude - the same encoding used for historical chart recognition.
    """
    if len(closes) < length:
        return None
    window = [float(x) for x in closes[-length:]]
    step = (length - 1) / (dims - 1)
    pts = [window[int(round(i * step))] for i in range(dims)]
    lo, hi = min(pts), max(pts)
    rng = hi - lo
    if rng <= 0:
        return None
    pts = [(p - lo) / rng for p in pts]
    mean = sum(pts) / dims
    pts = [p - mean for p in pts]
    norm = math.sqrt(sum(p * p for p in pts))
    if norm <= 1e-9:
        return None
    return [p / norm for p in pts]


class PatternMemory:
    """Library of (chart shape at entry -> what that trade actually did)."""

    def __init__(self, shapes=None, buy_r=None, sell_r=None, meta=None):
        self.shapes = shapes            # (n, DIMS) float32
        self.buy_r = buy_r              # realised R had the trade been a BUY
        self.sell_r = sell_r            # realised R had it been a SELL
        self.meta = meta or {}

    # ------------------------------------------------------------ loading
    @classmethod
    def load(cls, path: str | None = None):
        if np is None:
            return cls()
        p = Path(path or DEFAULT_PATH)
        if not p.exists():
            return cls()
        try:
            z = np.load(p, allow_pickle=False)
            meta_path = p.with_suffix(".meta.json")
            meta = {}
            if meta_path.exists():
                meta = json.loads(meta_path.read_text())
            return cls(z["shapes"], z["buy_r"], z["sell_r"], meta)
        except Exception:
            return cls()

    @property
    def size(self) -> int:
        return 0 if self.shapes is None else int(self.shapes.shape[0])

    def ready(self, minimum: int = 500) -> bool:
        return np is not None and self.size >= minimum

    # ------------------------------------------------------------ query
    def query(self, shape, k: int = 50) -> dict | None:
        """Expected R for BOTH sides from the k most similar past setups.

        Returns {'buy': float, 'sell': float, 'n': k} or None when unusable.
        """
        if not self.ready() or shape is None:
            return None
        q = np.asarray(shape, dtype=np.float32)
        if q.shape[0] != self.shapes.shape[1]:
            return None
        sim = self.shapes @ q                      # cosine, both unit-normed
        kk = int(min(k, sim.shape[0] - 1))
        if kk <= 0:
            return None
        top = np.argpartition(-sim, kk)[:kk]
        buy_values = self.buy_r[top]
        sell_values = self.sell_r[top]
        buy = float(np.nanmean(buy_values)) if np.isfinite(buy_values).any() else 0.0
        sell = float(np.nanmean(sell_values)) if np.isfinite(sell_values).any() else 0.0
        return {"buy": buy, "sell": sell, "n": kk}

    _write_lock = threading.Lock()

    def observe(self, shape, side: str, result_r: float, *, max_rows: int = 10000) -> bool:
        # Closed-trade feedback updates future memory only. It never mutates
        # an approved proposal and never participates in current entry authority.
        if np is None or shape is None or str(side).upper() not in {"BUY", "SELL"}:
            return False
        try:
            vector = np.asarray(shape, dtype=np.float32)
            if vector.ndim != 1 or vector.shape[0] != DIMS:
                return False
            value = float(result_r)
        except (TypeError, ValueError):
            return False
        with self._write_lock:
            current = PatternMemory.load()
            shapes = np.asarray(current.shapes, dtype=np.float32) if current.shapes is not None else np.empty((0, DIMS), dtype=np.float32)
            buy = np.asarray(current.buy_r, dtype=np.float32) if current.buy_r is not None else np.empty((0,), dtype=np.float32)
            sell = np.asarray(current.sell_r, dtype=np.float32) if current.sell_r is not None else np.empty((0,), dtype=np.float32)
            if shapes.ndim != 2 or shapes.shape[1] != DIMS:
                return False
            row_buy = value if str(side).upper() == "BUY" else np.nan
            row_sell = value if str(side).upper() == "SELL" else np.nan
            shapes = np.concatenate((shapes, vector.reshape(1, DIMS)), axis=0)
            buy = np.concatenate((buy, np.asarray([row_buy], dtype=np.float32)))
            sell = np.concatenate((sell, np.asarray([row_sell], dtype=np.float32)))
            if len(shapes) > max_rows:
                shapes, buy, sell = shapes[-max_rows:], buy[-max_rows:], sell[-max_rows:]
            path = Path(DEFAULT_PATH)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.stem + ".tmp.npz")
            np.savez_compressed(temporary, shapes=shapes, buy_r=buy, sell_r=sell)
            temporary.replace(path)
            meta_path = path.with_suffix(".meta.json")
            meta = dict(current.meta or {})
            meta["observations"] = int(len(shapes))
            meta_path.write_text(json.dumps(meta, sort_keys=True))
            return True
