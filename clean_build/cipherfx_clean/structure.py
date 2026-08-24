"""Compatibility-free public alias to the canonical structure observer."""

from __future__ import annotations

from .intelligence.model import StructureState as Structure
from .intelligence.structure import analyse_structure as analyse
from .intelligence.model import SwingPoint as Swing

__all__ = ["Structure", "Swing", "analyse"]
