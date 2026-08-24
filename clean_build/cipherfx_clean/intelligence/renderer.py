"""Dependency-free SVG chart renderer for auditable chart evidence."""

from __future__ import annotations

from html import escape
from typing import Mapping, Sequence

from ..market import Candle


def render_svg(
    candles: tuple[Candle, ...],
    width: int = 900,
    height: int = 420,
    annotations: Sequence[Mapping[str, object]] = (),
) -> str:
    if not candles:
        raise ValueError("cannot render an empty chart")
    low = min(item.low for item in candles)
    high = max(item.high for item in candles)
    span = max(high - low, 1e-9)
    chart_height = height - 40
    chart_width = width - 40
    body_width = max(2.0, chart_width / len(candles) * 0.65)
    y = lambda price: 20 + (high - price) / span * chart_height
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">']
    parts.append('<rect width="100%" height="100%" fill="#081018"/>')
    for annotation in annotations:
        kind = str(annotation.get("type") or "UNKNOWN")
        if "low" in annotation and "high" in annotation:
            zone_low = float(annotation["low"])
            zone_high = float(annotation["high"])
            top, bottom = sorted((y(zone_high), y(zone_low)))
            color = (
                "#ef6262" if kind == "SUPPLY"
                else "#24c7a5" if kind == "DEMAND"
                else "#4b8cf7"
            )
            parts.append(
                f'<rect data-kind="{escape(kind)}" x="20" y="{top:.2f}" '
                f'width="{chart_width}" height="{max(bottom - top, 1):.2f}" '
                f'fill="{color}" fill-opacity="0.12" stroke="{color}" stroke-opacity="0.55"/>'
            )
        elif "price" in annotation:
            price = float(annotation["price"])
            color = {
                "ENTRY": "#4b8cf7",
                "STOP_LOSS": "#ef6262",
                "TAKE_PROFIT": "#24c7a5",
                "VWAP": "#f6c453",
                "STRUCTURE": "#d9e5f2",
            }.get(kind, "#9aa9bc" if kind.startswith("SESSION_") else "#f6c453")
            parts.append(
                f'<line data-kind="{escape(kind)}" x1="20" x2="{width - 20}" '
                f'y1="{y(price):.2f}" y2="{y(price):.2f}" stroke="{color}" '
                'stroke-width="1" stroke-dasharray="5 4"/>'
            )
            if kind in {"ENTRY", "STOP_LOSS", "TAKE_PROFIT", "VWAP"}:
                parts.append(
                    f'<text data-kind="{escape(kind)}_LABEL" x="{width - 118}" '
                    f'y="{max(12.0, y(price) - 3):.2f}" fill="{color}" font-size="10">'
                    f'{escape(kind.replace("_", " "))}</text>'
                )
    for index, candle in enumerate(candles):
        x = 20 + chart_width * (index + 0.5) / len(candles)
        color = "#24c7a5" if candle.close >= candle.open else "#ef6262"
        parts.append(f'<line x1="{x:.2f}" x2="{x:.2f}" y1="{y(candle.high):.2f}" y2="{y(candle.low):.2f}" stroke="{color}"/>')
        top, bottom = sorted((y(candle.open), y(candle.close)))
        parts.append(f'<rect x="{x - body_width / 2:.2f}" y="{top:.2f}" width="{body_width:.2f}" height="{max(bottom - top, 1):.2f}" fill="{color}"/>')
    structure = next(
        (item for item in annotations if item.get("type") == "STRUCTURE"),
        None,
    )
    structure_label = ""
    if structure is not None:
        structure_label = f" | STRUCTURE {escape(str(structure.get('direction') or 'UNKNOWN'))}"
    parts.append(
        f'<text data-kind="STRUCTURE_LABEL" x="20" y="{height - 10}" fill="#d9e5f2">'
        f'{escape(candles[0].symbol)} {escape(candles[0].timeframe)}{structure_label}</text>'
    )
    parts.append("</svg>")
    return "".join(parts)
