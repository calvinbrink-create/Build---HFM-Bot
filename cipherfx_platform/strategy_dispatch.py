from __future__ import annotations

from .contracts import MarketSnapshot

def build_setup(snapshot: MarketSnapshot) -> dict:
    if snapshot.asset_class == "metal":
        from .engines.metals import build_setup as build_metals_setup
        return build_metals_setup(snapshot)
    if snapshot.asset_class == "index":
        from .engines.indices import build_setup as build_indices_setup
        return build_indices_setup(snapshot)
    from .engines.forex import build_setup as build_forex_setup
    return build_forex_setup(snapshot)
