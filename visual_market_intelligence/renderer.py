"""Deterministic evidence renderer; never fetches future bars or external images."""
from __future__ import annotations
from pathlib import Path
from typing import Any, Iterable, Mapping

def render_candles_png(candles: Iterable[Mapping[str, Any]], output_path: str | Path,
                       *, width: int = 1200, height: int = 700,
                       title: str = "CipherFX causal evidence") -> str:
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise RuntimeError("Pillow is required for deterministic PNG rendering") from exc
    rows = list(candles)
    if not rows:
        raise ValueError("at least one completed candle is required")
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (width, height), "#10151c")
    draw = ImageDraw.Draw(image)
    draw.text((24, 18), title, fill="#dbe5ef")
    left, top, right, bottom = 45, 60, width - 25, height - 35
    highs = [float(row["high"]) for row in rows]
    lows = [float(row["low"]) for row in rows]
    hi, lo = max(highs), min(lows)
    span = max(hi - lo, 1e-12)
    step = (right - left) / max(len(rows), 1)
    for index, row in enumerate(rows):
        o, h, l, c = (float(row[key]) for key in ("open", "high", "low", "close"))
        x = left + (index + .5) * step
        y = lambda price: bottom - (price - lo) / span * (bottom - top)
        color = "#35c48b" if c >= o else "#ef6c75"
        draw.line((x, y(h), x, y(l)), fill=color, width=1)
        draw.rectangle((x - max(1, step * .28), y(max(o, c)), x + max(1, step * .28), y(min(o, c))), fill=color)
    image.save(output, format="PNG", optimize=False)
    return str(output)
