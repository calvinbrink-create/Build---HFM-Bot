from __future__ import annotations

from dataclasses import dataclass
from .contracts import MarketAnalysis, ScoreResult, SIDES, utc_now


@dataclass(frozen=True)
class ScoreProfile:
    minimum_total: float = 70.0


class ScoringEngine:
    """Converts LearningEngine facts into scores; it never reads candles."""

    WEIGHTS = {
        "forex": {
            "htf": {"ema": 30.0, "rmi": 25.0, "slope": 20.0, "adx": 15.0, "location": 10.0},
            "m15": {"poi": 30.0, "choch": 25.0, "bos": 25.0, "retracement": 20.0},
            "index_m15": {"session_poi": 25.0, "session_break": 30.0, "range_retest": 25.0, "momentum": 20.0},
            "m5": {"break_or_retest": 45.0, "direction": 20.0, "body": 20.0, "range": 15.0},
        },
        "index": {
            "htf": {"ema": 25.0, "session": 25.0, "range": 20.0, "adx": 20.0, "location": 10.0},
            "m15": {"session_poi": 25.0, "session_break": 30.0, "range_retest": 25.0, "momentum": 20.0},
            "m5": {"break_or_retest": 40.0, "direction": 20.0, "body": 20.0, "range": 20.0},
        },
        "metal": {
            "htf": {"ema": 20.0, "volatility": 30.0, "rmi": 25.0, "location": 15.0, "pressure": 10.0},
            "m15": {"volatility_poi": 25.0, "impulse": 30.0, "retracement": 20.0, "volatility_reclaim": 25.0},
            "m5": {"break_or_retest": 35.0, "direction": 25.0, "body": 20.0, "range": 20.0},
        },
    }

    @staticmethod
    def _score(features: dict[str, bool], weights: dict[str, float], aliases: dict[str, tuple[str, ...]] | None = None) -> float:
        aliases = aliases or {}
        total = 0.0
        for component, weight in weights.items():
            keys = aliases.get(component, (component,))
            if any(bool(features.get(key)) for key in keys):
                total += weight
        return round(total, 2)

    def score(self, analysis: MarketAnalysis, profile: ScoreProfile | None = None) -> ScoreResult:
        profile = profile or ScoreProfile()
        weights = self.WEIGHTS.get(analysis.asset_class, self.WEIGHTS["forex"])
        htf_weights = weights["htf"]
        htf_aliases = {
            "ema": ("ema_stack_buy", "ema_stack_sell", "trend_stack"),
            "rmi": ("rmi_buy", "rmi_sell"),
            "slope": ("slope_buy", "slope_sell"),
            "adx": ("adx_regime",),
            "location": ("location_buy", "location_sell"),
            "session": ("session_bias",),
            "range": ("range_expansion",),
            "volatility": ("volatility_expansion",),
            "pressure": ("candle_pressure_buy", "candle_pressure_sell"),
        }
        h4_score = self._score(analysis.h4.features, htf_weights, htf_aliases)
        h1_score = self._score(analysis.h1.features, htf_weights, htf_aliases)
        m15_weights = weights["m15"] if analysis.asset_class != "forex" or "poi" in analysis.m15.features else weights["m15"]
        m15_score = self._score(analysis.m15.features, m15_weights)
        m5_score = self._score(analysis.m5.features, weights["m5"])
        htf_aligned = analysis.h4.side in SIDES and analysis.h1.side == analysis.h4.side
        m15_aligned = htf_aligned and analysis.m15.side == analysis.h4.side
        components = {
            "htf_alignment": 25.0 if htf_aligned else 0.0,
            "m15_structure": 15.0 * min(m15_score, 100.0) / 100.0 if htf_aligned else 0.0,
            "m5_trigger": 25.0 * min(m5_score, 100.0) / 100.0 if m15_aligned else 0.0,
            "regime": 10.0 if htf_aligned else 0.0,
            "liquidity": 5.0 if m15_aligned else 0.0,
            "reward_cost_room": 10.0 if analysis.stop_distance > 0 else 0.0,
            "data_quality": 10.0,
        }
        total = round(sum(components.values()), 2)
        reasons = []
        if analysis.h4.side not in SIDES:
            reasons.append("H4_NO_DIRECTION")
        elif analysis.h1.side != analysis.h4.side:
            reasons.append("H1_NOT_ALIGNED")
        elif analysis.m15.side != analysis.h4.side:
            reasons.append("M15_NOT_ALIGNED")
        elif analysis.m5.side != analysis.h4.side:
            reasons.append("M5_TRIGGER_NOT_READY")
        if total < profile.minimum_total:
            reasons.append("STRATEGY_SCORE_BELOW_MINIMUM")
        return ScoreResult(
            analysis.symbol, analysis.asset_class, total, profile.minimum_total,
            bool(not reasons), components,
            {"H4": h4_score, "H1": h1_score, "M15": m15_score, "M5": m5_score},
            tuple(dict.fromkeys(reasons)), utc_now(),
        )
