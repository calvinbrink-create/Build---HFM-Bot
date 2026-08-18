from __future__ import annotations

import json
import os
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mt5_xm_gateway import MT5Gateway

from .database import DatabaseLayer


class _MemBar:
    """Minimal OHLC holder so the entry engine's encoder can read exit-side bars."""
    __slots__ = ("open", "high", "low", "close")

    def __init__(self, o, h, l, c):
        self.open, self.high, self.low, self.close = o, h, l, c



class TradeManagementEngine:
    """Monitors and manages open broker positions only."""

    def __init__(self, gateway: MT5Gateway, database: DatabaseLayer):
        self.gateway = gateway
        self.database = database
        profile_path = Path(
            os.getenv(
                "CIPHERFX_PROFILE_PATH",
                str(Path(__file__).resolve().parent.parent / "config" / "symbol_profiles.json"),
            )
        )
        try:
            payload = json.loads(profile_path.read_text())
            self.profiles = payload.get("symbols", {})
        except (OSError, json.JSONDecodeError):
            self.profiles = {}
        # symbol_profiles.json is keyed by canonical name (NAS100, XAGUSD),
        # but live broker positions use the broker alias (US100Cash, SILVER)
        # - confirmed live 2026-07-22: US100Cash ran 13h+ past its 8h cap
        # because _profile("US100Cash") found nothing (NAS100<->US100Cash
        # share no common substring, so the old CASH-suffix-strip fallback
        # below never caught it). Build the real reverse alias map instead
        # of guessing at suffix patterns.
        self._broker_to_canonical: dict[str, str] = {}
        try:
            symbols_path = Path(
                os.getenv("MT5_SYMBOLS_FILE", str(Path(__file__).resolve().parent.parent / "mt5_symbols.json"))
            )
            aliases = json.loads(symbols_path.read_text()).get("aliases", {})
            for canonical, broker_name in aliases.items():
                self._broker_to_canonical[str(broker_name).upper()] = str(canonical).upper()
        except (OSError, json.JSONDecodeError):
            pass
        missing_loss_cap = sorted(
            symbol
            for symbol, profile in self.profiles.items()
            if isinstance(profile, dict) and float(profile.get("max_open_position_loss_usd", 0.0) or 0.0) <= 0.0
        )
        if missing_loss_cap:
            self.database.event(
                "SYMBOL_PROFILE_MISSING_LOSS_CAP",
                {
                    "symbols": missing_loss_cap,
                    "reason": "max_open_position_loss_usd is absent or zero for these symbols - "
                              "the per-position dollar loss auto-close safety net is disabled for them",
                },
            )
        # Raised from 0.30/0.50/0.60: moving to breakeven/trailing this early
        # was capping winners at ~0.5R average while the entry logic designs
        # for a 1.6-1.8:1 reward:risk target - these defaults now give a
        # trade room to develop before protecting profit, per standard
        # practice (wait for >=1R, ideally more, before breakeven).
        self.default_breakeven_r = float(os.getenv("MT5_MANAGEMENT_BREAKEVEN_R", "1.0"))
        self.default_trail_start_r = float(os.getenv("MT5_MANAGEMENT_TRAIL_START_R", "1.2"))
        self.default_profit_take_r = max(
            0.0, float(os.getenv("MT5_PROFIT_TAKE_R", "1.5"))
        )
        self.trail_distance_r = max(
            0.05, float(os.getenv("MT5_MANAGEMENT_TRAIL_DISTANCE_R", "0.25"))
        )
        # Ratcheting trail: as peak profit grows, the giveback allowed from
        # that peak shrinks, so a big winner cannot bleed back to breakeven.
        # Format "peak:giveback,peak:giveback" in R. Validated over 16 years:
        # baseline (no trail) 40% win/+0.180R; 0.5:0.30,1.0:0.20,1.5:0.10 ->
        # 65% win/+0.046R; 0.3:0.20,0.7:0.12,1.2:0.06 -> 77% win/+0.026R.
        # Higher win rate, lower total R - tighten or loosen to taste.
        # Empty string disables it and restores the old fixed-distance trail.
        # Manual/orphan position adoption: a position found with NO broker
        # stop loss (typically opened by hand in the MT5 terminal) gets a
        # protective stop and target attached from the same ATR geometry the
        # engines use, then flows into normal management. Previously such a
        # position was simply CLOSED, which meant a hand-placed trade was
        # killed instead of protected. Set MT5_ADOPT_UNSTOPPED=0 to restore
        # the old close-it behaviour.
        self.adopt_unstopped = str(os.getenv("MT5_ADOPT_UNSTOPPED", "1")).strip().lower() in {"1","true","yes","on"}
        self.adopt_atr_timeframe = os.getenv("MT5_ADOPT_ATR_TIMEFRAME", "H1")
        self.adopt_atr_period = int(float(os.getenv("MT5_ADOPT_ATR_PERIOD", "14")))
        self.adopt_stop_atr_mult = float(os.getenv("MT5_ADOPT_STOP_ATR_MULT", "1.30"))
        self.adopt_target_r = float(os.getenv("MT5_ADOPT_TARGET_R", "1.60"))
        self.ratchet_tiers = []
        raw_tiers = os.getenv("MT5_RATCHET_TIERS", "0.5:0.30,1.0:0.20,1.5:0.10").strip()
        for chunk in raw_tiers.split(","):
            chunk = chunk.strip()
            if not chunk or ":" not in chunk:
                continue
            try:
                peak_s, give_s = chunk.split(":", 1)
                self.ratchet_tiers.append((float(peak_s), float(give_s)))
            except ValueError:
                continue
        self.ratchet_tiers.sort(key=lambda t: t[0])
        self.duration_profit_lock_buffer_seconds = max(
            0.0, float(os.getenv("MT5_DURATION_PROFIT_LOCK_BUFFER_SECONDS", "1800"))
        )
        self.early_exit_r = float(os.getenv("MT5_MANAGEMENT_EARLY_EXIT_R", "-0.75"))
        # Candle-structure exit. Until now NOTHING in this file read a candle:
        # every exit came from P/L, an R ratio, or a clock, so the bot could not
        # tell a green marubozu from a green bar with a 60% upper wick.
        #
        # Measured 2026-08-13, production engines, M5 execution, identical trade
        # lists per mode, unresolved trades marked to market, 60/40 holdout:
        #
        #   SDZONE / US30 (directional)   train expR / t   holdout expR / t
        #     ratchet only                 +0.0214 +0.83    +0.1472 +5.11
        #     ratchet + reject-in-profit   +0.0453 +2.25    +0.1419 +5.78
        #
        # Banking on rejection beats the plain ratchet in-sample and matches it
        # out of sample with a HIGHER t (less variance). On FADE/UK100 the same
        # rule measured NEGATIVE in both halves (-0.0201 / -0.0191) because a
        # fade buys weakness by design - a pullback is the normal path of its
        # winners, not failure. So this is scoped to directional symbols only
        # and UK100 is deliberately excluded.
        #
        # In profit ONLY. Cutting losers on the same signal measured worse than
        # the ratchet on both engines (UK100 -0.0230, and it drops the win rate
        # to 26.5% by closing trades that were coming back).
        self.structure_exit_enabled = str(
            os.getenv("MT5_STRUCTURE_EXIT_ENABLE", "1")
        ).strip().lower() not in {"0", "false", "no"}
        self.memory_exit_enabled = str(
            os.getenv("MT5_MEMORY_EXIT_ENABLED", "1")
        ).strip().lower() not in {"0", "false", "no"}
        self.memory_exit_timeframe = os.getenv("MT5_MEMORY_EXIT_TIMEFRAME", "M1")
        self.memory_exit_min_samples = int(os.getenv("MT5_MEMORY_EXIT_MIN_SAMPLES", "200"))
        self.memory_exit_edge = float(os.getenv("MT5_MEMORY_EXIT_EDGE", "0.02"))
        # While history says the move continues, the stop trails this far behind
        # LIVE price (in ATR) instead of sitting a fixed giveback under the peak,
        # so it keeps climbing and the trade is free to run past 1R.
        self.memory_exit_trail_atr = float(os.getenv("MT5_MEMORY_EXIT_TRAIL_ATR", "1.5"))
        # When history says it turns, take the giveback in instead.
        self.memory_exit_cut_mult = float(os.getenv("MT5_MEMORY_EXIT_CUT_MULT", "0.5"))
        self._memory_cache = {}
        self.structure_wick = float(os.getenv("MT5_STRUCTURE_EXIT_WICK", "0.50"))
        self.structure_body = float(os.getenv("MT5_STRUCTURE_EXIT_BODY", "0.30"))
        # M1, not M5: the rejection is read off the last COMPLETED bar, so the
        # timeframe IS the detection lag. On M5 a peak could be up to 5 minutes
        # gone before the bot was allowed to see it.
        self.structure_timeframe = os.getenv("MT5_STRUCTURE_EXIT_TIMEFRAME", "M1")
        # Immediate giveback-from-peak. monitor() runs every MT5_POLL_SECONDS
        # (0.2s live), and mfe_r is the true high-water mark, so this does NOT
        # wait for any candle to close - the moment price hands back a fraction
        # of the best profit the trade ever showed, it is out.
        #
        # The existing giveback guard cannot do this job: it arms at 2.00R and
        # allows 0.50R back, and trades that peak at ~0.2R never reach it. Live
        # UK100 2026-08-13 peaked at 0.225R (+8,982 ZAR) and banked 2,344 with
        # nothing firing.
        self.peak_giveback_enabled = str(
            os.getenv("MT5_PEAK_GIVEBACK_ENABLE", "1")
        ).strip().lower() not in {"0", "false", "no"}
        # fraction of peak profit allowed back before closing
        self.peak_giveback_frac = max(0.01, min(0.95, float(
            os.getenv("MT5_PEAK_GIVEBACK_FRACTION", "0.33")
        )))
        # ignore noise below this peak - do not exit a trade that never moved
        self.peak_giveback_min_r = max(0.0, float(
            os.getenv("MT5_PEAK_GIVEBACK_MIN_R", "0.08")
        ))
        # HARD profit exit - take the money at a high point instead of waiting
        # for a pullback to prove the move is over.
        #
        # Derived from Calvin's own fills on 2026-08-13, which is how he trades
        # by hand and what he asked the bot to copy:
        #   USA30  6 legs x 30 lots @ 53737 -> out @ 53730 in 16s  = +16,504 ZAR
        #   XAUUSD 8 bot legs @ 4363.02     -> out @ 4363.52 in 32s = +7,815 ZAR
        # In R terms against the current stops that is 0.275R on USA30, 0.229R
        # on USA100, 0.357R on USA500, 0.074R on XAUUSD, 0.098R on UK100.
        #
        # This is deliberately NOT routed through _effective_profit_take_r,
        # which returns max(cap, the order's own target) and therefore can only
        # ever move a take-profit further away, never nearer.
        # 0 disables it and the trade runs to its target as before.
        self.hard_profit_r = max(0.0, float(os.getenv("MT5_HARD_PROFIT_R", "0.25")))
        # Giveback guard: protects a favorable move before breakeven-lock
        # (1.0R) would otherwise kick in. Was fully configured in env but
        # never wired into any code path - between 0 and 1.0R a trade could
        # run to +0.9R and reverse all the way to a loss with nothing
        # stopping it. Confirmed live 2026-07-20 user request.
        self.giveback_guard_enabled = str(os.getenv("MT5_GIVEBACK_GUARD_ENABLE", "1")).strip().lower() not in {"0", "false", "no"}
        self.giveback_trigger_r = float(os.getenv("MT5_GIVEBACK_TRIGGER_R", "0.70"))
        self.giveback_max_r = max(0.01, float(os.getenv("MT5_GIVEBACK_MAX_R", "0.20")))
        # Recovery mode: explicit instruction 2026-07-22, after AUDUSD closed
        # -$399 (0.75R on a ~$530 risk position). Only activates once a
        # position is already CONFIRMED in trouble (adverse move past the
        # trouble threshold) - this does not touch fresh/healthy trades, so
        # it doesn't reintroduce the scalp-cut bug fixed earlier (that fix
        # was about not cutting trades tight relative to their OWN risk;
        # this is a separate, later-stage safety net only for trades that
        # already proved they're struggling). Once triggered: grab
        # breakeven the instant price returns to it (don't wait for the
        # normal 1.0R breakeven trigger), and if it never recovers, cap the
        # eventual loss at a flat dollar figure instead of the R-based
        # early-exit, which scales up with position size.
        self.recovery_mode_enabled = str(os.getenv("MT5_RECOVERY_MODE_ENABLE", "1")).strip().lower() not in {"0", "false", "no"}
        self.recovery_trouble_r = float(os.getenv("MT5_RECOVERY_TROUBLE_R", "0.30"))
        self.recovery_max_loss_usd = max(0.0, float(os.getenv("MT5_RECOVERY_MAX_LOSS_USD", "150")))
        self.modify_retry_seconds = max(
            5.0, float(os.getenv("MT5_MANAGEMENT_MODIFY_RETRY_SECONDS", "30"))
        )
        self.close_retry_seconds = max(
            5.0, float(os.getenv("MT5_MANAGEMENT_CLOSE_RETRY_SECONDS", "30"))
        )
        self.monitor_event_seconds = max(
            1.0, float(os.getenv("MT5_MANAGEMENT_EVENT_INTERVAL_SECONDS", "10"))
        )
        self.management_max_tick_age_seconds = max(
            30.0, float(os.getenv("MT5_MANAGEMENT_MAX_TICK_AGE_SECONDS", "60"))
        )
        self._last_portfolio_event_at = 0.0

    def _profile(self, symbol: str) -> dict[str, Any]:
        key = str(symbol).upper()
        candidates = [key]
        canonical = self._broker_to_canonical.get(key)
        if canonical:
            candidates.append(canonical)
        if key.endswith("CASH"):
            candidates.append(key[:-4])
        if key.endswith(".CASH"):
            candidates.append(key[:-5])
        for candidate in candidates:
            profile = self.profiles.get(candidate)
            if isinstance(profile, dict):
                return profile
        return {}

    def _effective_profit_take_r(self, position, state, default_r: float) -> float:
        """Profit-take level that never cuts a trade below its OWN target.

        Audit 2026-07-30: the CRT engine ships a 3R take-profit, but management
        closed at the profile/global profit_take_r of 1.3-1.5R first, so the 3R
        target was unreachable and every CRT winner was cut roughly in half.
        The validated CRT backtest ran with no profit-take cap at all.

        The order already carries its designed target on the broker side, so
        that distance IS the intent. Take the larger of the configured cap and
        the target the order was actually built with - a trade whose own TP is
        nearer than the cap is unaffected.
        """
        configured = self._profile_float(position.symbol, "profit_take_r", default_r)
        try:
            tp = float(position.tp or 0.0)
            entry = float(state.get("initial_entry", position.price_open))
            initial_sl = float(state.get("initial_sl", position.sl) or 0.0)
            risk_distance = abs(initial_sl - entry)
            if tp > 0 and risk_distance > 0:
                own_target_r = abs(tp - entry) / risk_distance
                if own_target_r > configured:
                    return own_target_r
        except (TypeError, ValueError, AttributeError):
            pass
        return configured

    def _hard_profit_r(self, position) -> float:
        """Per-symbol hard profit target, in R.

        Defaults are the R levels Calvin's own exits landed on, so the bot cuts
        roughly where he cuts rather than holding for a fixed multiple that
        means something different on every instrument.
        MT5_HARD_PROFIT_R_<CANONICAL> overrides one symbol,
        MT5_HARD_PROFIT_R is the fallback for anything unlisted.
        """
        raw = str(getattr(position, "symbol", "") or "").upper()
        canonical = self._broker_to_canonical.get(raw, raw)
        override = os.getenv(f"MT5_HARD_PROFIT_R_{canonical}")
        if override is not None:
            try:
                return max(0.0, float(override))
            except (TypeError, ValueError):
                pass
        return self.hard_profit_r

    def _memory_bias(self, position):
        """What the 7.9-year memory says about the shape in front of it NOW.

        The entry side asks this before opening a trade. The exit side asked
        nothing - it counted R and moved a stop, so a pullback inside a healthy
        trend and a genuine reversal looked identical to it. That is why winners
        came in at 0.35R against 1.00R losses.

        Returns (bias, key, n, up_rate, atr):
             +1  history says this shape keeps going the way the trade faces
             -1  history says it turns against the trade
              0  no lean, too few samples, or no data - behave exactly as before

        Uses the SAME encoder as the entry engine, imported rather than copied,
        so the two halves can never drift apart. Cached per symbol per bar.
        """
        blank = (0, "", 0, None, 0.0)
        if not self.memory_exit_enabled:
            return blank
        try:
            from .engines.quick_scalp import MARKET_MEMORY, memory_state
        except Exception:
            return blank
        if not MARKET_MEMORY:
            return blank
        symbol = str(getattr(position, "symbol", "") or "")
        buy = str(getattr(position, "direction", "")).upper() == "BUY"
        try:
            frame = self.gateway.rates(symbol, self.memory_exit_timeframe, 200)
        except Exception:
            return blank
        if frame is None or len(frame) < 130:
            return blank
        stamp = str(frame.index[-1])
        hit = self._memory_cache.get(symbol)
        if hit is not None and hit[0] == stamp:
            key, rec, atr = hit[1], hit[2], hit[3]
        else:
            try:
                candles = [
                    _MemBar(float(o), float(h), float(l), float(c))
                    for o, h, l, c in zip(
                        frame["Open"].tolist(), frame["High"].tolist(),
                        frame["Low"].tolist(), frame["Close"].tolist(),
                    )
                ]
            except Exception:
                return blank
            trs = []
            for i in range(len(candles) - 14, len(candles)):
                h, l, pc = candles[i].high, candles[i].low, candles[i - 1].close
                trs.append(max(h - l, abs(h - pc), abs(l - pc)))
            atr = sum(trs) / len(trs) if trs else 0.0
            got = memory_state(candles, MARKET_MEMORY)
            if got is None:
                self._memory_cache[symbol] = (stamp, "", None, atr)
                return blank
            key, rec = got
            self._memory_cache[symbol] = (stamp, key, rec, atr)
        if not rec or int(rec.get("n", 0)) < self.memory_exit_min_samples:
            return 0, key, int((rec or {}).get("n", 0)), None, atr
        up = float(rec["up_rate"])
        edge = self.memory_exit_edge
        n = int(rec["n"])
        if up >= 0.5 + edge:
            return (1 if buy else -1), key, n, up, atr
        if up <= 0.5 - edge:
            return (-1 if buy else 1), key, n, up, atr
        return 0, key, n, up, atr

    def _structure_exit_reason(self, position, current_r, peak_r=None) -> str:
        """Bank profit when the candles say the move has been rejected.

        Returns a close reason, or "" to leave the position alone. Fails CLOSED
        (returns "") on any missing or bad data - it must never close a trade
        because a rates call hiccuped.
        """
        if not self.structure_exit_enabled:
            return ""
        if current_r is None or float(current_r) <= 0.0:
            return ""                       # in profit only
        # There must be REAL profit to protect. Without this the rule fired on
        # any tick of green: live 2026-08-13 15:01:08 it closed UK100 on
        # STRUCTURE_LOWER_HIGHS with mfe_r=0.0098R - a trade that had barely
        # left the entry price, 30 seconds after opening. A trade has to be
        # given room to actually run before candle structure means anything.
        if float(peak_r if peak_r is not None else current_r) < self.peak_giveback_min_r:
            return ""
        raw = str(getattr(position, "symbol", "") or "").upper()
        canonical = self._broker_to_canonical.get(raw, raw)
        # "*" (the default) = EVERY engine, EVERY symbol. A pullback closes the
        # trade wherever it happens - explicit instruction 2026-08-13. A comma
        # list still works if it ever needs narrowing again.
        raw_allowed = os.getenv("MT5_STRUCTURE_EXIT_SYMBOLS", "*").strip()
        if raw_allowed != "*":
            allowed = {s.strip().upper() for s in raw_allowed.split(",") if s.strip()}
            if canonical not in allowed:
                return ""
        try:
            frame = self.gateway.rates(position.symbol, self.structure_timeframe, 8)
        except Exception:
            return ""
        if frame is None or len(frame) < 4:
            return ""
        try:
            o = [float(x) for x in frame["Open"].tolist()]
            h = [float(x) for x in frame["High"].tolist()]
            lo = [float(x) for x in frame["Low"].tolist()]
            c = [float(x) for x in frame["Close"].tolist()]
        except Exception:
            return ""
        i = len(c) - 2                      # last COMPLETED bar; -1 may be forming
        if i < 2:
            return ""
        buy = str(getattr(position, "direction", "")).upper() == "BUY"
        rng = h[i] - lo[i]
        if rng > 0:
            body = abs(c[i] - o[i]) / rng
            wick = ((h[i] - max(o[i], c[i])) if buy else (min(o[i], c[i]) - lo[i])) / rng
            if wick >= self.structure_wick and body <= self.structure_body:
                return f"STRUCTURE_REJECTION_WICK_{wick:.2f}"
        if buy and h[i] < h[i - 1] < h[i - 2]:
            return "STRUCTURE_LOWER_HIGHS"
        if (not buy) and lo[i] > lo[i - 1] > lo[i - 2]:
            return "STRUCTURE_HIGHER_LOWS"
        return ""

    def _duration_lock_worth_taking(self, position, current_r: float) -> bool:
        """Is the profit at the duration cap actually worth banking?

        DURATION_PROFIT_LOCK previously fired on ANY profit above zero, which
        produced closes like USDJPY +$5.45 (R=+0.097) after a 7.5 hour hold -
        a result indistinguishable from noise, and small enough that a single
        round trip of commission or a slightly worse fill wipes it out.
        Explicit instruction 2026-07-30: do not close for less than the floor.

        Below the floor the trade is left alone and simply runs on to its hard
        MAX_DURATION_EXCEEDED cutoff, still protected by its stop, the ratchet
        and the giveback guard - so the downside is bounded either way.

        Both floors must be cleared: MT5_DURATION_LOCK_MIN_USD (dollars) and
        MT5_DURATION_LOCK_MIN_R (size-independent). Set either to 0 to disable
        that half of the check.
        """
        min_usd = max(0.0, float(os.getenv("MT5_DURATION_LOCK_MIN_USD", "10")))
        min_r = max(0.0, float(os.getenv("MT5_DURATION_LOCK_MIN_R", "0.20")))
        if min_usd > 0:
            try:
                if float(position.profit or 0.0) < min_usd:
                    return False
            except (TypeError, ValueError):
                return False
        if min_r > 0 and current_r < min_r:
            return False
        return True

    def _profile_tiers(self, symbol: str):
        """Per-symbol ratchet tiers, falling back to the global env tiers.

        A symbol whose giveback guard has been widened to let big moves run
        (US30) has nothing watching profit below the first global tier at
        1.0R - a trade could peak at +0.78R and round-trip to zero with no
        protection. Profile tiers let that symbol lock profit EARLIER via the
        trailing stop without capping the upside the way an early close does.
        Format in symbol_profiles.json: "ratchet_tiers": [[0.5,0.25],[1.0,0.4]]
        """
        raw = self._profile(symbol).get("ratchet_tiers")
        if not raw:
            return self.ratchet_tiers
        try:
            tiers = sorted((float(a), float(b)) for a, b in raw)
            return tiers or self.ratchet_tiers
        except (TypeError, ValueError):
            return self.ratchet_tiers

    def _profile_float(self, symbol: str, field: str, default: float) -> float:
        value = self._profile(symbol).get(field, default)
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    @staticmethod
    def _session(now: datetime) -> str:
        hour = now.hour
        if 0 <= hour < 7:
            return "Asia"
        if 7 <= hour < 12:
            return "London"
        if 12 <= hour < 16:
            return "overlap"
        if 16 <= hour < 22:
            return "New York"
        return "unknown"

    @staticmethod
    def _r_multiple(position, initial_sl: float) -> float | None:
        risk = abs(float(position.price_open) - float(initial_sl))
        if risk <= 0:
            return None
        if str(position.direction).upper() == "BUY":
            return (float(position.price_current) - float(position.price_open)) / risk
        return (float(position.price_open) - float(position.price_current)) / risk

    @staticmethod
    def _price_volatility(history: list[float]) -> float | None:
        if len(history) < 3:
            return None
        changes = [
            float(history[index]) - float(history[index - 1])
            for index in range(1, len(history))
        ]
        return float(statistics.pstdev(changes)) if changes else None

    @staticmethod
    def _rank(current_r: float | None, drawdown_r: float, spread_jump: bool) -> str:
        if current_r is None:
            return "CRITICAL"
        if current_r <= -1.0 or (spread_jump and current_r < 0):
            return "CRITICAL"
        if current_r <= -0.75:
            return "DANGER"
        if current_r < -0.20:
            return "WEAK"
        if current_r < 0.20:
            return "NEUTRAL"
        if current_r < 0.50:
            return "HEALTHY"
        if drawdown_r > 0.35:
            return "HEALTHY"
        if current_r < 1.0:
            return "STRONG"
        return "EXCELLENT"

    def _quote(self, symbol: str):
        try:
            tick_reader = getattr(self.gateway, "position_symbol_tick", None)
            info_reader = getattr(self.gateway, "position_symbol_info", None)
            _, tick = (tick_reader or self.gateway.symbol_tick)(symbol)
            spec = (info_reader or self.gateway.symbol_info)(symbol)
            points = (float(tick.ask) - float(tick.bid)) / max(float(spec.point), 1e-12)
            return points, float(tick.bid), float(tick.ask), spec
        except Exception:
            return None, None, None, None

    def _fresh_action_quote(self, symbol: str) -> tuple[bool, str]:
        # Do not send close/modify requests against a closed-market quote.
        try:
            reader = getattr(self.gateway, "position_symbol_tick", None)
            if not callable(reader):
                reader = self.gateway.symbol_tick
            _, tick = reader(symbol)
            raw_time = getattr(tick, "time_utc", getattr(tick, "time", 0))
            if isinstance(raw_time, datetime):
                stamp = raw_time if raw_time.tzinfo else raw_time.replace(tzinfo=timezone.utc)
                stamp = stamp.astimezone(timezone.utc)
            else:
                stamp = datetime.fromtimestamp(float(raw_time), tz=timezone.utc)
            age = max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds())
            if age > self.management_max_tick_age_seconds:
                return False, f"STALE_POSITION_QUOTE:{age:.1f}s"
            return True, ""
        except Exception as exc:
            return False, f"POSITION_QUOTE_UNAVAILABLE:{type(exc).__name__}"

    def _defer_operational_action(self, position, state: dict[str, Any], action: str, reason: str) -> None:
        now = datetime.now(timezone.utc)
        previous = state.get("last_operational_defer_at")
        if previous:
            try:
                if (now - datetime.fromisoformat(str(previous))).total_seconds() < self.monitor_event_seconds:
                    return
            except ValueError:
                pass
        state["last_operational_defer_at"] = now.isoformat()
        state["last_operational_defer_reason"] = reason
        self.database.event(
            "POSITION_ACTION_DEFERRED",
            {
                "ticket": int(getattr(position, "ticket", 0) or 0),
                "symbol": str(getattr(position, "symbol", "") or ""),
                "action": action,
                "reason": reason,
            },
            symbol=str(getattr(position, "symbol", "") or ""),
        )

    def _action(self, position, state: dict[str, Any], action: str, reason: str, **extra):
        payload = {
            "ticket": int(position.ticket),
            "symbol": position.symbol,
            "side": position.direction,
            "action": action,
            "reason": reason,
            **extra,
        }
        self.database.event(
            "POSITION_MANAGEMENT_ACTION",
            payload,
            symbol=position.symbol,
        )
        state["last_action"] = action
        state["last_action_reason"] = reason
        state["last_action_at"] = datetime.now(timezone.utc).isoformat()
        action_log = list(state.get("action_log", []))
        action_log.append(dict(payload, action_at=state["last_action_at"]))
        state["action_log"] = action_log[-100:]

    def _close(self, position, state: dict[str, Any], reason: str, metrics: dict[str, Any]) -> None:
        fresh, quote_reason = self._fresh_action_quote(position.symbol)
        if not fresh:
            self._defer_operational_action(position, state, "CLOSE", quote_reason)
            return
        now = datetime.now(timezone.utc)
        failed_at = state.get("last_close_attempt_at")
        if failed_at:
            try:
                age = (now - datetime.fromisoformat(str(failed_at))).total_seconds()
            except ValueError:
                age = self.close_retry_seconds
            if age < self.close_retry_seconds:
                return
        try:
            self.gateway.close_position(position)
            state["close_requested"] = True
            state.pop("last_close_attempt_at", None)
            state.pop("last_close_error", None)
            self._action(position, state, "CLOSE", reason, metrics=metrics)
        except Exception as exc:
            state["last_close_attempt_at"] = now.isoformat()
            state["last_close_error"] = f"{type(exc).__name__}:{str(exc)[:180]}"
            if "Position not found" in str(exc):
                state["close_requested"] = True
                self.database.event(
                    "POSITION_ACTION_SUPERSEDED",
                    {
                        "ticket": int(position.ticket),
                        "symbol": position.symbol,
                        "action": "CLOSE",
                        "reason": "POSITION_NOT_FOUND_AT_SUBMISSION",
                    },
                    symbol=position.symbol,
                )
                return
            self.database.event(
                "POSITION_MANAGEMENT_ERROR",
                {
                    "ticket": int(position.ticket),
                    "symbol": position.symbol,
                    "action": "CLOSE",
                    "reason": reason,
                    "error": state["last_close_error"],
                    "retry_after_seconds": self.close_retry_seconds,
                },
                symbol=position.symbol,
            )


    def _adoption_stop_distance(self, symbol: str) -> float | None:
        """ATR-based stop distance for adopting a position that has no stop."""
        try:
            frame = self.gateway.rates(symbol, self.adopt_atr_timeframe, self.adopt_atr_period + 40)
        except Exception:
            return None
        if frame is None or len(frame) < self.adopt_atr_period + 2:
            return None
        try:
            highs = [float(x) for x in frame["High"].tolist()]
            lows = [float(x) for x in frame["Low"].tolist()]
            closes = [float(x) for x in frame["Close"].tolist()]
        except Exception:
            return None
        ranges = []
        for i in range(len(closes) - self.adopt_atr_period, len(closes)):
            if i < 1:
                continue
            ranges.append(max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])))
        if not ranges:
            return None
        atr = sum(ranges) / len(ranges)
        return atr * self.adopt_stop_atr_mult if atr > 0 else None

    def _adopt_unstopped_position(self, position, state, metrics) -> bool:
        """Attach SL/TP to a position that has none. True if adopted."""
        entry = float(state.get("initial_entry", position.price_open) or position.price_open)
        if entry <= 0:
            return False
        # If the broker already has a stop on this position, the trader set it
        # by hand - never overwrite it. Just seed our own baseline from it so
        # the position becomes managed (R-multiples, ratchet, duration cap all
        # need initial_sl) without touching their protection.
        live_sl = float(getattr(position, "sl", 0.0) or 0.0)
        if live_sl > 0:
            state["initial_sl"] = live_sl
            state["initial_entry"] = entry
            state["adopted"] = True
            state["adopted_at"] = datetime.now(timezone.utc).isoformat()
            self._action(position, state, "ADOPT_EXISTING_STOP",
                         "MANUAL_POSITION_ADOPTED_KEPT_STOP", sl=live_sl, metrics=metrics)
            return True
        distance = self._adoption_stop_distance(position.symbol)
        if not distance or distance <= 0:
            return False
        buy = str(position.direction).upper() == "BUY"
        new_sl = entry - distance if buy else entry + distance
        new_tp = entry + distance * self.adopt_target_r if buy else entry - distance * self.adopt_target_r
        # Never attach a stop that is already breached - that would fire
        # instantly. Anchor off current price instead so the trade gets a
        # real protective distance from where it actually is now.
        current = float(position.price_current)
        if (buy and new_sl >= current) or ((not buy) and new_sl <= current):
            new_sl = current - distance if buy else current + distance
            new_tp = current + distance * self.adopt_target_r if buy else current - distance * self.adopt_target_r
        try:
            self.gateway.modify_position(
                int(position.ticket), position.symbol, float(new_sl), float(new_tp)
            )
        except Exception as exc:
            self.database.event(
                "POSITION_ADOPTION_FAILED",
                {"ticket": int(position.ticket), "symbol": position.symbol,
                 "error": f"{type(exc).__name__}:{str(exc)[:160]}"},
                symbol=position.symbol,
            )
            return False
        state["initial_sl"] = float(new_sl)
        state["initial_entry"] = entry
        state["adopted"] = True
        state["adopted_at"] = datetime.now(timezone.utc).isoformat()
        self._action(position, state, "ADOPT_UNSTOPPED", "MANUAL_POSITION_ADOPTED",
                     sl=float(new_sl), tp=float(new_tp), stop_distance=float(distance), metrics=metrics)
        return True

    def _modify(self, position, state: dict[str, Any], new_sl: float, reason: str, metrics):
        fresh, quote_reason = self._fresh_action_quote(position.symbol)
        if not fresh:
            self._defer_operational_action(position, state, "MODIFY_SL", quote_reason)
            return
        now = datetime.now(timezone.utc)
        failed_target = state.get("last_failed_sl")
        failed_at = state.get("last_modify_attempt_at")
        if failed_target is not None and failed_at:
            try:
                age = (now - datetime.fromisoformat(str(failed_at))).total_seconds()
            except ValueError:
                age = self.modify_retry_seconds
            # Was keyed on the exact SL value. Because the minimum-distance
            # clamp tracks price, every retry produced a slightly different
            # target and the backoff never engaged - 22 rejected attempts in
            # 24 seconds on one ticket. Back off on the TICKET instead.
            if age < self.modify_retry_seconds:
                return
        try:
            info_reader = getattr(self.gateway, "position_symbol_info", None)
            spec = (info_reader or self.gateway.symbol_info)(position.symbol)
            normalizer = getattr(self.gateway, "normalize_price", None)
            if normalizer is None:
                digits = max(0, int(getattr(spec, "digits", 8) or 8))
                normalizer = lambda value, price: round(float(price), digits)
            new_sl = float(normalizer(spec, new_sl))
            minimum_distance = float(getattr(spec, "stops_level", 0) or 0) * float(
                getattr(spec, "point", 0.0) or 0.0
            )
            _, bid, ask, _ = self._quote(position.symbol)
            # Clamping to EXACTLY stops_level sends the stop sitting on the
            # broker's minimum. The quote read here is a moment older than the
            # order that follows, so any tick against us in between puts the
            # stop inside the minimum and the broker returns 4756. Measured
            # live 2026-08-14 on the USA30 burst: 40 of 72 MODIFY_SL rejected,
            # one ticket refused 22 times in 24s while its profit bled away.
            # A buffer absorbs that gap. MT5_STOPS_LEVEL_BUFFER=1.0 restores
            # the old edge-of-minimum behaviour.
            buffer_mult = max(1.0, float(os.getenv("MT5_STOPS_LEVEL_BUFFER", "2.5")))
            safe_distance = minimum_distance * buffer_mult
            if safe_distance > 0 and bid is not None and ask is not None:
                if position.direction == "BUY":
                    new_sl = min(new_sl, float(bid) - safe_distance)
                else:
                    new_sl = max(new_sl, float(ask) + safe_distance)
                new_sl = float(normalizer(spec, new_sl))
            # The broker-minimum-distance clamp above reads a second, later
            # quote than the one the caller used to decide this modify
            # improves the stop. An adverse tick between those two reads can
            # clamp new_sl past the position's current live stop - re-check
            # here so we never submit a modify that widens risk instead of
            # protecting it.
            current_sl = float(getattr(position, "sl", 0.0) or 0.0)
            if current_sl > 0:
                still_improves = (
                    new_sl > current_sl if position.direction == "BUY" else new_sl < current_sl
                )
                if not still_improves:
                    return
            self.gateway.modify_position(
                int(position.ticket), position.symbol, float(new_sl), float(position.tp)
            )
            state["last_managed_sl"] = float(new_sl)
            state.pop("last_failed_sl", None)
            state.pop("last_modify_error", None)
            self._action(
                position,
                state,
                "MODIFY_SL",
                reason,
                new_sl=float(new_sl),
                metrics=metrics,
            )
        except Exception as exc:
            state["last_failed_sl"] = float(new_sl)
            state["last_modify_attempt_at"] = now.isoformat()
            state["last_modify_error"] = f"{type(exc).__name__}:{str(exc)[:180]}"
            self.database.event(
                "POSITION_MANAGEMENT_ERROR",
                {
                    "ticket": int(position.ticket),
                    "symbol": position.symbol,
                    "attempted_sl": float(new_sl),
                    "current_sl": float(getattr(position, "sl", 0.0) or 0.0),
                    "action": "MODIFY_SL",
                    "reason": reason,
                    "error": state["last_modify_error"],
                    "retry_after_seconds": self.modify_retry_seconds,
                },
                symbol=position.symbol,
            )

    def _initial_state(self, position, now: datetime) -> dict[str, Any]:
        baseline = self.database.position_baseline(int(position.ticket)) or {}
        entry = float(baseline.get("entry_price") or position.price_open)
        initial_sl = float(baseline.get("stop_loss") or position.sl)
        initial_tp = float(baseline.get("take_profit") or position.tp)
        return {
            "ticket": int(position.ticket),
            "symbol": position.symbol,
            "side": position.direction,
            "initial_entry": entry,
            "initial_sl": initial_sl,
            "initial_tp": initial_tp,
            "initial_risk_amount": float(baseline.get("initial_risk") or 0.0),
            "first_seen_at": now.isoformat(),
            "last_seen_at": now.isoformat(),
            "last_price": float(position.price_current),
            "price_history": [float(position.price_current)],
            "last_spread_points": None,
            "mfe_r": 0.0,
            "mae_r": 0.0,
            "last_managed_sl": float(position.sl),
            "close_requested": False,
            "last_action": "NONE",
            "action_log": [],
        }

    def _monitor_position(
        self,
        position,
        total_positions: int,
        portfolio: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        state = self.database.load_management_state(int(position.ticket))
        if not state:
            state = self._initial_state(position, now)
        # Repair a stale/racey baseline: the execution's position_ticket can be
        # 0 at the instant management first snapshots a just-filled position
        # (it is reconciled to the real broker ticket a moment later), so
        # position_baseline() returns nothing and initial_risk_amount gets
        # cached as 0 forever. That zero silently disables the risk-scaled
        # loss cap AND the R-based exits, letting a swing trade get scalp-cut
        # at the tiny fixed dollar cap. Re-fetch from the baseline while the
        # cached risk/stop are still missing.
        if float(state.get("initial_risk_amount", 0.0) or 0.0) <= 0.0 or float(state.get("initial_sl", 0.0) or 0.0) <= 0.0:
            baseline = self.database.position_baseline(int(position.ticket)) or {}
            repaired_risk = float(baseline.get("initial_risk") or 0.0)
            repaired_sl = float(baseline.get("stop_loss") or 0.0)
            if repaired_risk > 0:
                state["initial_risk_amount"] = repaired_risk
            if repaired_sl > 0 and float(state.get("initial_sl", 0.0) or 0.0) <= 0.0:
                state["initial_sl"] = repaired_sl

        initial_sl = float(state.get("initial_sl", 0.0) or 0.0)
        current_r = self._r_multiple(position, initial_sl)
        previous_price = float(state.get("last_price", position.price_current) or position.price_current)
        spread_points, bid, ask, spec = self._quote(position.symbol)
        previous_spread = state.get("last_spread_points")
        spread_jump = (
            spread_points is not None
            and previous_spread is not None
            and spread_points > float(previous_spread) * 1.5
        )
        quote_delta = float(position.price_current) - previous_price
        favorable_delta = (
            quote_delta if str(position.direction).upper() == "BUY" else -quote_delta
        )
        peak_r = max(float(state.get("mfe_r", 0.0) or 0.0), float(current_r or 0.0))
        prior_mae_r = abs(float(state.get("mae_r", 0.0) or 0.0))
        adverse_r = max(prior_mae_r, max(0.0, -float(current_r or 0.0)))
        drawdown_r = max(0.0, peak_r - float(current_r or 0.0))
        price_history = [
            float(value) for value in state.get("price_history", [])
            if isinstance(value, (int, float))
        ]
        price_history.append(float(position.price_current))
        price_history = price_history[-120:]
        volatility = self._price_volatility(price_history)
        previous_r = state.get("last_r")
        velocity_r = (
            float(current_r) - float(previous_r)
            if current_r is not None and previous_r is not None
            else 0.0
        )
        previous_velocity = float(state.get("last_velocity_r", 0.0) or 0.0)
        profit_acceleration_r = velocity_r - previous_velocity
        recent_changes = [
            price_history[index] - price_history[index - 1]
            for index in range(max(1, len(price_history) - 4), len(price_history))
        ]
        favorable_changes = [
            change if str(position.direction).upper() == "BUY" else -change
            for change in recent_changes
        ]
        favorable_change_count = sum(change > 0 for change in favorable_changes)
        trend_continuation = (
            "FAVORABLE"
            if favorable_changes and favorable_change_count >= len(favorable_changes) / 2
            else "ADVERSE"
            if favorable_changes
            else "UNKNOWN"
        )
        try:
            opened_at = position.time
            time_in_trade = max(0.0, (now - opened_at).total_seconds())
        except Exception:
            time_in_trade = 0.0
        time_decay = (
            "NEGATIVE"
            if time_in_trade >= 300 and (current_r or 0.0) <= 0.0
            else "POSITIVE"
            if (current_r or 0.0) > 0.0
            else "NEUTRAL"
        )

        metrics = {
            "ticket": int(position.ticket),
            "symbol": position.symbol,
            "side": position.direction,
            "current_profit": float(position.profit),
            "r_multiple": current_r,
            "drawdown_r": drawdown_r,
            "mfe_r": peak_r,
            "mae_r": adverse_r,
            "time_in_trade_seconds": time_in_trade,
            "spread_points": spread_points,
            "spread_changed": spread_jump,
            "quote_delta": quote_delta,
            "price_change_direction": (
                "FAVORABLE" if favorable_delta > 0 else "ADVERSE" if favorable_delta < 0 else "FLAT"
            ),
            "distance_to_tp": abs(float(position.tp) - float(position.price_current)),
            "distance_to_sl": abs(float(position.price_current) - float(position.sl)),
            "price_risk_exposure": abs(float(position.price_open) - initial_sl) * float(position.volume),
            "portfolio_open_positions": total_positions,
            "session": self._session(now),
            "volatility": volatility,
            "liquidity_event": "SPREAD_EXPANSION" if spread_jump else None,
            "momentum": favorable_delta,
            "market_momentum": trend_continuation,
            "trend_continuation": trend_continuation,
            "profit_velocity_r": velocity_r,
            "profit_acceleration_r": profit_acceleration_r,
            "time_decay": time_decay,
            "market_behavior": "SPREAD_EXPANSION" if spread_jump else trend_continuation,
            "initial_risk_amount": float(state.get("initial_risk_amount", 0.0) or 0.0),
            "max_open_position_loss_usd": self._profile_float(position.symbol, "max_open_position_loss_usd", 0.0),
            "recovery_status": (
                "RECOVERY_MONITORING" if current_r is not None and current_r < 0
                else "PROFIT_PROTECTION" if current_r is not None and current_r > 0
                else "NEUTRAL"
            ),
            "account": dict(portfolio or {}),
        }
        rank = self._rank(current_r, drawdown_r, spread_jump)
        metrics["rank"] = rank

        if not state.get("close_requested"):
            # Active management is observation-only after entry. The immutable
            # broker SL/TP owns the trade geometry; this path does not close,
            # trail, ratchet, or resize a position after it is opened.
            live_sl = float(getattr(position, "sl", 0.0) or 0.0)
            if initial_sl <= 0 or live_sl <= 0:
                # A position with no broker stop is unmanaged. Keep the
                # operational safeguard so an orphan cannot remain unprotected.
                if self.adopt_unstopped and self._adopt_unstopped_position(position, state, metrics):
                    pass
                elif self.recovery_mode_enabled and current_r is not None and current_r >= 0.0:
                    self._close(position, state, "RECOVERY_BREAKEVEN_GRAB_NO_STOP", metrics)
                elif (
                    self.recovery_mode_enabled
                    and self.recovery_max_loss_usd > 0
                    and float(position.profit) <= -self.recovery_max_loss_usd
                ):
                    self._close(position, state, "RECOVERY_LOSS_CAPPED_NO_STOP", metrics)
                elif not self.recovery_mode_enabled:
                    self._close(position, state, "UNMANAGED_POSITION_NO_STOP", metrics)
            elif (
                (position.direction == "BUY" and position.price_current <= live_sl)
                or (position.direction == "SELL" and position.price_current >= live_sl)
            ):
                # The broker should normally have closed it; this only cleans
                # up a still-open position after a stop-breach is observed.
                self._close(position, state, "STOP_BREACHED_POSITION_STILL_OPEN", metrics)
            elif (
                current_r is not None
                and self._hard_profit_r(position) > 0.0
                and float(current_r) >= self._hard_profit_r(position)
            ):
                self._close(
                    position,
                    state,
                    f"HARD_PROFIT_{float(current_r):.3f}R",
                    metrics,
                )
            elif current_r is not None and current_r > 0.0 and initial_sl > 0.0:
                # Restore the positive-side ratchet only. It locks profitable
                # movement in the broker stop and never closes or loosens risk
                # while the position is losing.
                tiers = self._profile_tiers(position.symbol)
                if tiers:
                    giveback = None
                    for tier_peak, tier_give in tiers:
                        if peak_r >= tier_peak:
                            giveback = float(tier_give)
                    if giveback is not None:
                        entry_price = float(state.get("initial_entry", position.price_open))
                        risk_distance = abs(initial_sl - entry_price)
                        locked_r = peak_r - giveback
                        desired_sl = (
                            entry_price + locked_r * risk_distance
                            if position.direction == "BUY"
                            else entry_price - locked_r * risk_distance
                        )
                        improves = (
                            desired_sl > live_sl
                            if position.direction == "BUY"
                            else desired_sl < live_sl
                        )
                        if improves:
                            self._modify(position, state, desired_sl, "PROFIT_RATCHET", metrics)

        last_monitor_event_at = state.get("last_monitor_event_at")
        emit_monitor_event = True
        if last_monitor_event_at:
            try:
                emit_monitor_event = (
                    now - datetime.fromisoformat(str(last_monitor_event_at))
                ).total_seconds() >= self.monitor_event_seconds
            except ValueError:
                emit_monitor_event = True
        if emit_monitor_event:
            state["last_monitor_event_at"] = now.isoformat()

        state.update(
            {
                "last_seen_at": now.isoformat(),
                "last_price": float(position.price_current),
                "last_spread_points": spread_points,
                "mfe_r": peak_r,
                "mae_r": adverse_r,
                "last_profit": float(position.profit),
                "price_history": price_history,
                "last_r": current_r,
                "last_velocity_r": velocity_r,
                "last_profit_acceleration_r": profit_acceleration_r,
                "last_metrics": metrics,
                "rank": rank,
                "time_in_trade_seconds": time_in_trade,
            }
        )
        self.database.save_management_state(int(position.ticket), state)
        if emit_monitor_event:
            self.database.event("POSITION_MONITOR", metrics, symbol=position.symbol)
        return metrics

    def _breakeven_buffer_price(self, symbol: str) -> float:
        # A stop parked at the exact entry tick gets whipsawed closed by
        # ordinary price noise within minutes (confirmed live 2026-07-20:
        # EU50Cash/EURUSD legs locked to exact breakeven closed for $0.00
        # 1-4 minutes later). Move the stop a few points past entry, in the
        # trade's favor, so normal noise can't touch it.
        points = max(0.0, float(os.getenv("MT5_BREAKEVEN_BUFFER_POINTS", "2")))
        if points <= 0:
            return 0.0
        try:
            spec = self.gateway.symbol_info(symbol)
            return points * float(spec.point)
        except Exception:
            return 0.0

    def _lock_earlier_pyramid_legs_to_breakeven(self, positions):
        # Each pyramid leg is its own independent MT5 position (own SL, own
        # exit cycle) - nothing else ties them together. Without this, a
        # later leg can hit its own stop and lose while an earlier, still-
        # profitable leg keeps running untouched, netting the "pyramid" to
        # roughly breakeven instead of compounding one correct trade idea.
        # Covers positions already open before this protection was added, in
        # addition to the same lock applied at fill time for new legs.
        if str(os.getenv("MT5_PYRAMID_LOCK_PRIOR_LEGS_BREAKEVEN", "1")).strip().lower() in {"0", "false", "no"}:
            return
        groups: dict[tuple[str, str], list] = {}
        for position in positions:
            key = (str(position.symbol).upper(), str(position.direction).upper())
            groups.setdefault(key, []).append(position)
        for (symbol, direction), members in groups.items():
            if len(members) < 2:
                continue
            members_with_time = [p for p in members if getattr(p, "time", None)]
            if len(members_with_time) < 2:
                continue
            newest = max(members_with_time, key=lambda p: p.time)
            for position in members_with_time:
                if position.ticket == newest.ticket:
                    continue
                if float(getattr(position, "profit", 0.0) or 0.0) <= 0.0:
                    continue
                entry = float(position.price_open)
                sl = float(position.sl)
                improves = entry > sl if direction == "BUY" else entry < sl
                if not improves:
                    continue
                buffer = self._breakeven_buffer_price(position.symbol)
                locked_sl = entry + buffer if direction == "BUY" else entry - buffer
                try:
                    self.gateway.modify_position(int(position.ticket), position.symbol, locked_sl, float(position.tp))
                    self.database.event(
                        "PYRAMID_PRIOR_LEG_BREAKEVEN_LOCKED",
                        {"sl": locked_sl, "reason": "existing_leg_sweep"},
                        symbol=position.symbol,
                    )
                except Exception as exc:
                    self.database.event(
                        "PYRAMID_BREAKEVEN_LOCK_FAILED",
                        {"ticket": int(position.ticket), "error": f"{type(exc).__name__}:{str(exc)[:180]}"},
                        symbol=position.symbol,
                    )

    def monitor(self):
        positions = self.gateway.positions()
        try:
            self._lock_earlier_pyramid_legs_to_breakeven(positions)
        except Exception as exc:
            self.database.event(
                "PYRAMID_BREAKEVEN_SWEEP_FAILED",
                {"error": f"{type(exc).__name__}:{str(exc)[:180]}"},
            )
        try:
            account = self.gateway.account_info()
            portfolio = {
                "equity": float(getattr(account, "equity", 0.0) or 0.0),
                "balance": float(getattr(account, "balance", 0.0) or 0.0),
                "margin": float(getattr(account, "margin", 0.0) or 0.0),
                "free_margin": float(getattr(account, "free_margin", 0.0) or 0.0),
                "margin_level": float(getattr(account, "margin_level", 0.0) or 0.0),
            }
        except Exception:
            portfolio = {}
        metrics = []
        for position in positions:
            try:
                metrics.append(self._monitor_position(position, len(positions), portfolio))
            except Exception as exc:
                # One bad position/state row must not stop trailing, breakeven,
                # stop-breach detection, and the $-loss cap for every other
                # currently open position this cycle.
                self.database.event(
                    "POSITION_MONITOR_FAILED",
                    {
                        "ticket": int(getattr(position, "ticket", 0) or 0),
                        "error": f"{type(exc).__name__}:{str(exc)[:180]}",
                    },
                    symbol=str(getattr(position, "symbol", "") or ""),
                )
        portfolio["open_positions"] = len(positions)
        portfolio["portfolio_heat_usd"] = sum(
            float(item.get("initial_risk_amount", 0.0) or 0.0) for item in metrics
        )
        self.database.status("open_positions", metrics)
        self.database.status("position_intelligence", metrics)
        if time.monotonic() - self._last_portfolio_event_at >= self.monitor_event_seconds:
            self.database.event(
                "POSITION_PORTFOLIO_MONITOR",
                {
                    "open_positions": len(positions),
                    "risk_exposure_price": sum(
                        float(item.get("price_risk_exposure", 0.0) or 0.0) for item in metrics
                    ),
                    "portfolio_heat_usd": portfolio["portfolio_heat_usd"],
                    "portfolio": portfolio,
                    "critical": sum(item.get("rank") == "CRITICAL" for item in metrics),
                    "danger": sum(item.get("rank") == "DANGER" for item in metrics),
                },
            )
            self._last_portfolio_event_at = time.monotonic()
        for position in positions:
            self.database.save_position(position)
        self.database.prune_closed_positions(
            {int(p.ticket) for p in positions},
            {str(p.symbol) for p in positions},
        )
        return positions
