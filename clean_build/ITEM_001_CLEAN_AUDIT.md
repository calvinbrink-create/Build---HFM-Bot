# Clean Build Item 001 Audit

This is a scoped code inventory for the isolated build under
`/opt/cipherfx_mt5/clean_build`. It is **not** a complete-build PASS and does
not certify any requirement merely because a named module exists. The master
267-requirement ledger remains the only end-to-end certification authority.

## Call path

Verified live read-only path:

`HFM tick export -> CsvTickObserver -> EvidenceStore raw_ticks -> rolling
SourceDataset + persisted tick window -> CompletionScheduler -> CleanRuntime
-> IntelligenceEngine.analyse -> HistoricalAnalogueIndex ->
StructuredResearchProvider -> immutable NO_TRADE -> EvidenceStore`

Executable decisions have a separately tested contract path:

`TradeDecision -> validate_decision -> BotExecutionHandoff -> injected
ExistingBotPort -> ExecutionReceipt`

That second path has no deployed MT5 adapter or live broker proof in the clean
build and therefore is not certified end to end.

## Implemented clean responsibilities

| Responsibility | Clean evidence |
|---|---|
| Raw bid/ask tick capture and quality | `market.py`, `store.py` |
| Completed multi-resolution candles | `market.py` (`1s`, `5s`, `15s`, `M1`, `M3`, `M5`, `M15`, `M30`, `H1`, `H4`, `D1`) |
| Immutable market snapshots | `contracts.py`, `snapshot.py`, `hfm_data.py` |
| Candle and chart evidence | `candles.py`, `charts.py`, `intelligence/chart.py`, `intelligence/renderer.py` |
| Structure, swings, BOS/CHOCH fields | `intelligence/structure.py` |
| Supply/demand lifecycle and quality | `intelligence/zones.py`, `intelligence/advanced.py` |
| Liquidity, sweeps, FVG and displacement | `intelligence/liquidity.py`, `intelligence/market_features.py` |
| Tick direction, imbalance, velocity, acceleration | `intelligence/market_features.py` |
| Spread, VWAP, activity and volatility observations | `intelligence/microstructure.py`, `intelligence/market_features.py`, `intelligence/advanced.py` |
| Sessions, session ranges and profiles | `intelligence/session.py`, `intelligence/market_features.py`, `intelligence/advanced.py` |
| Regime labels and empirical probabilities | `intelligence/regime.py`, `intelligence/advanced.py` |
| Price-action, SMC, Wyckoff, breakout, reversion and momentum vocabulary | `intelligence/strategies.py`, `intelligence/knowledge.py` |
| Geometric chart patterns and failure conditions | `intelligence/patterns.py` |
| Feature store and whole-market state library | `intelligence/feature_store.py`, `intelligence/data_library.py` |
| Analogue search and fingerprints | `historical_memory.py`, `features.py` |
| Forward outcomes, MFE/MAE, time-to-event, TP-before-SL | `intelligence/outcomes.py` |
| Costs, executable bid/ask fills and latency | `intelligence/execution_model.py`, `intelligence/costs.py`, `intelligence/latency.py` |
| Hypothesis discovery and deterministic experiments | `intelligence/edge_miner.py`, `intelligence/hypothesis.py` |
| Purged/embargoed walk-forward and OOS evidence | `intelligence/validation.py`, `intelligence/validation_full.py` |
| Multiple-test and overfit evidence | `intelligence/research.py` |
| Feature/filter/governor attribution | `intelligence/attribution.py` |
| Strategy graveyard and versioned edge governance | `intelligence/edge_validation.py`, `intelligence/governance.py` |
| Shadow tournament, drift and live monitoring contracts | `intelligence/tournament.py`, `intelligence/monitor.py` |
| Structured intelligence input/output and gateway | `intelligence/runtime_contracts.py`, `intelligence/provider.py` |
| Operational validation and existing-bot handoff boundary | `operations.py`, `mt5_boundary.py`, `execution.py` |
| Position management and post-trade feedback | `management.py`, `feedback.py` |
| Deterministic replay and executable-price backtest | `replay.py`, `backtest.py` |
| Read-only dashboard evidence projection | `dashboard.py` |
| SQLite evidence persistence | `store.py` |

## Boundary proof

- No clean module imports `cipherfx_platform`, `mt5_xm_gateway`,
  `CipherFxBridge`, an old engine, or a fallback strategy.
- Intelligence has no broker import or order call.
- Execution receives an immutable decision and forwards it exactly once to the
  injected existing-bot port. It does not construct or place broker orders.
- The MT5 adapter is a protocol only. No MT5 client, direct broker port,
  account, credentials, or production configuration is copied into the clean
  build.
- The research layer can return `BUY`, `SELL`, or `NO_TRADE`; no hidden score,
  timeframe unanimity rule, daily cap, or undocumented veto is introduced.
- The bot process remained stopped. MT5 being open is not treated as proof of
  bot execution.

## VPS validation evidence

Command:

```text
cd /opt/cipherfx_mt5
PYTHONPATH=clean_build /opt/cipherfx_mt5/.venv_mt5/bin/python -m pytest -q clean_build/tests
/opt/cipherfx_mt5/.venv_mt5/bin/python -m compileall -q clean_build
```

Latest complete clean-build suite: **367 tests passed**. The active compact
historical population contains 3,723 clean states and corresponding
bar-fidelity paths across five symbols. The live collector has processed all
five symbols without a broker event.

The package remains a read-only research build, not a production trading
deployment. Historical executable bid/ask ticks, measured cost evidence,
shadow-forward duration, a deployed MT5 execution adapter, and real broker
receipts remain separate requirements and are intentionally not claimed.
