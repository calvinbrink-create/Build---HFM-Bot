# Recovered Friday Scope Ledger

Source task: `019f4b50-7771-79f1-a695-8b27c54738d0`

Source window: 2026-08-21 08:05-20:10 SAST. This ledger converts the
requirements discussed during that window into auditable build obligations.
It is a requirements document, not implementation evidence. The previous
`trading_knowledge_base` files may be inspected to recover intent, but they
MUST NOT be imported, copied, or used as runtime implementation by the clean
build.

| ID | Required clean-build capability | Acceptance evidence |
|---|---|---|
| F00 | Market mechanics knowledge | Versioned knowledge records cover price discovery, auctions, spread, volatility, gaps, and market states; records are used by the intelligence request. |
| F01 | Participants, liquidity, and price discovery | Evidence distinguishes observable price/liquidity facts from inferred participant intent. |
| F02 | Complete candle, wick, and candle-pattern library | Deterministic tests cover bullish, bearish, neutral, reversal, continuation, and failed patterns over completed candles. |
| F03 | Structure, zones, and liquidity | Runtime emits swings, trend/range state, BOS/CHOCH observations, supply/demand lifecycle, liquidity pools, sweeps, and invalidations. |
| F04 | Multiple strategy/framework families | Library includes both directions and no-trade hypotheses across trend, breakout, reversal, mean-reversion, liquidity, session, volatility, and chart-pattern families. |
| F05 | Asset, session, calendar, and regime context | Symbol/session/regime context is explicit and historically testable; no one-rule-fits-all symbol logic. |
| F06 | Execution, costs, and risk evidence | Research outcomes use executable bid/ask, spread, slippage, latency, commission, swap, margin, and broker constraints. |
| F07 | Append-only evidence ledger | Every fact, inference, setup, proposal, execution, outcome, and validation result has stable identity and provenance. |
| F08 | Whole-market setup memory | Memory stores ordinary states, winners, losses, breakevens, rejected, expired, missed, failed-pattern, and no-trade observations. |
| F09 | Framework library | Framework definitions are versioned hypotheses, not hidden gates, scores, or guaranteed strategies. |
| F10 | Regime- and symbol-specific research | Results are sliced by symbol, session, regime, timeframe, volatility, spread, and direction. |
| F11 | Candidate generation | Candidate hypotheses originate from structured evidence and remain separate from execution. |
| F12 | Leakage-safe walk-forward validation | Chronological train/test/holdout, purge, embargo, overlap control, uncertainty, and OOS evidence are reproducible. |
| F13 | Memory population | Historical states and forward outcomes populate persistent clean storage with source hashes. |
| F14 | Dataset control | Dataset identity, content hashes, coverage, revisions, and rejected inputs are retained. |
| F15 | Research orchestration | One runtime coordinates evidence, memory, analogues, hypotheses, validation, governance, and decisions without dormant branches. |
| F16 | Research certification | Certification requires explicit evidence thresholds and cannot silently activate live execution. |
| F17 | Read-only HFM/MT5 data import | Adapter discovers broker symbols/timeframes and imports without modifying terminal, account, or production bot. |
| F18 | Data quality | OHLC validity, ordering, duplicates, gaps, sufficiency, freshness, and completed-bar status are explicit. No synthetic repairs. |
| F19 | Exploratory research | Broad observations and candidate discovery are separated from confirmation and untouched holdout evidence. |
| F20 | Session evidence | Session/open/overlap/close observations use explicit timezone-aware calendars and symbol context. |
| F21 | HFM calendar reconciliation | Broker sessions, holidays, daylight-saving changes, and market closures are represented. |
| F22 | Holiday reconciliation | Holiday and shortened-session evidence cannot be silently treated as ordinary trading days. |
| F23 | Clean validation windows | Invalid, stale, incomplete, overlapping, or contaminated windows are reported and excluded from certification. |
| F24 | Timestamp provenance | Source timezone, terminal timezone, UTC conversion, SAST presentation, and conversion lineage are explicit. |
| F25 | Timestamp alignment | Ticks, candles, states, proposals, orders, executions, and outcomes reconcile to one UTC timeline. |
| F26 | Research readiness gate | Gate reports missing evidence; it cannot create or veto a trade and cannot hide failures. |
| F27 | Timestamp contract evidence | Automated tests prove DST, offset, boundary, and completed-candle behavior. |
| F28 | Terminal-time audit | Runtime can compare terminal/server/UTC/SAST timestamps without guessing offsets. |
| F29 | Evidence-backed setup construction | Setup is immutable, directional, identifies framework, context, trigger, entry, invalidation, target, expiry, and evidence; no hidden score. |
| F30 | Immutable research proposal | Proposal preserves setup identity, dataset/source identity, edge version, timestamp, and execution status. |
| F31 | Complete outcome learning | Closed, expired, missed, execution-failed, rejected, and no-trade counterfactual outcomes retain MFE, MAE, holding time, costs, and R. |
| F32 | Reconciled research reports | Reports expose unresolved, orphaned, duplicate, and identity-mismatched records. |
| F33 | Proposal walk-forward validation | Proposal/outcome records are evaluated chronologically with untouched holdout evidence. |
| F34 | Proposal certification | Certification is deterministic, versioned, and fails closed on missing or mismatched research evidence. |
| F35 | Reproducible release bundle | Dataset, code, configuration, evidence, reports, and certification hashes form one release identity. |
| F36 | Independent release verification | A separate verifier detects missing, mismatched, modified, or blocked artifacts. |
| F37 | End-to-end research pipeline | A single command reproduces evidence through release verification without live activation. |
| F38 | HFM research adapter | Broker export adapter preserves symbol, timeframe, source hash, timestamps, and rejected inputs. |
| F39 | HFM dataset gate | HFM inputs must satisfy provenance, quality, coverage, and timeframe requirements before research. |
| F40 | Complete market snapshot | Snapshot combines required broker timeframes as one observed state while retaining independent timeframe evidence; absent frames are explicit. |
| F41 | HFM evidence builder | Fresh completed frames produce candle, indicator, structure, liquidity, regime, zone, volatility, and session observations. |
| F42 | HFM chart-pattern builder | Forming, confirmed, failed, and conflicting patterns remain evidence; patterns do not approve trades by themselves. |
| F43 | HFM setup adapter | Only aligned, fresh, provenance-valid evidence may construct a setup; failures have explicit reasons. |
| F44 | HFM proposal adapter | Only valid immutable setups become proposals; duplicates and identity mismatches are rejected. |
| F45 | HFM outcome adapter | Broker and counterfactual outcomes reconcile to the originating proposal and dataset. |
| F46 | HFM research reporting | HFM-specific reports preserve unresolved, orphaned, duplicate, mismatch, and cost evidence. |
| F47 | HFM walk-forward adapter | HFM records run through chronological, leakage-safe train/test/holdout evaluation. |
| F48 | HFM certification adapter | HFM provenance and validation evidence are mandatory; certification remains separate from execution. |
| F49 | HFM release bundle | The HFM release includes source exports, hashes, adapters, reports, validation, and certification. |
| F50 | HFM release verification | Independent verification reproduces hashes and refuses incomplete or altered HFM evidence. |
| F51 | HFM pipeline | HFM import through verification is executable as one deterministic, restart-safe pipeline. |
| F52 | Timeframe derivation provenance | Any derived timeframe identifies source bars and derivation algorithm; broker-native and derived bars cannot be confused. |
| F53 | Universal configuration sweep | Candidate combinations cover permitted symbols, directions, contexts, timeframes, sessions, regimes, entries, and exits without declaring every configuration valid. |
| F54 | Edge discovery platform | Discovery controls multiple testing, records every experiment, and retains failed hypotheses in a graveyard. |
| F55 | Temporal setup research | Day, time, session, gap, duration, horizon, and market-open effects are measured as hypotheses. |
| F56 | Temporal candidate generation | Temporal rules are versioned candidates and cannot become hidden hard-coded blockers. |
| F57 | Adaptive setup memory | Memory weighting may update future research from realized evidence, but cannot rewrite approved proposals or bypass governance. |
| F58 | VPS chart rendering | The VPS can render reproducible annotated broker charts tied to evidence IDs and source timestamps. |
| F59 | Five-symbol research coverage | XAUUSD, UK100, USA100, USA500, and USA30 are inventoried; broker availability and missing symbols are explicit. |
| F60 | Frequent setup evaluation | Scheduler can evaluate newly completed configured bars, including five-minute cadence where applicable, without duplicate decisions. |
| F61 | Existing bot remains sole order authority | Intelligence sends immutable approved instructions; only the existing bot talks to MT5 for submit/modify/close. |
| F62 | No old strategy contamination | Clean static and runtime traces prove no import or call into old engines, old strategies, old fallbacks, or prior config authorities. |
| F63 | No guaranteed edge claim | Profitability is reported only from cost-adjusted OOS and forward evidence; `NO_EDGE` is a valid result. |
| F64 | One end-to-end traced runtime | HFM data to evidence to memory to validation to structured decision to bot receipt to outcome is one observable call path. |
