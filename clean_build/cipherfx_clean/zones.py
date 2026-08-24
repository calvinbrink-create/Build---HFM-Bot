"""Public aliases to the canonical zone observer."""

from __future__ import annotations

from .intelligence.model import Zone
from .intelligence.zones import identify_zones as discover

__all__ = ["Zone", "discover"]
