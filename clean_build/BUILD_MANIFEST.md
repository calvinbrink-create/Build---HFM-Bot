# Clean Build Manifest

Canonical implementation directory: `/opt/cipherfx_mt5/clean_build`

This directory is the single clean build. The earlier partial and Friday
folders remain outside the active path only as preserved audit references. No
module in this directory imports or copies them.

## Layers

| Layer | Modules |
|---|---|
| Contracts and market data | `contracts.py`, `market.py`, `features.py`, `store.py` |
| Chart and price intelligence | `candles.py`, `charts.py`, `intelligence/chart.py`, `intelligence/renderer.py`, `intelligence/patterns.py` |
| Market observations | `intelligence/structure.py`, `zones.py`, `liquidity.py`, `microstructure.py`, `market_features.py`, `advanced.py`, `session.py`, `regime.py`, `cross_asset.py` |
| State and memory | `memory.py`, `analogue.py`, `intelligence/data_library.py`, `intelligence/feature_store.py`, `intelligence/library.py`, `intelligence/lineage.py` |
| Research and validation | `intelligence/research.py`, `outcomes.py`, `edge_miner.py`, `hypothesis.py`, `validation.py`, `validation_full.py`, `edge_validation.py`, `execution_model.py`, `backtest.py`, `attribution.py` |
| Strategy vocabulary | `intelligence/knowledge.py`, `intelligence/strategies.py`, `intelligence/router.py`, `intelligence/planning.py` |
| Governance and monitoring | `intelligence/governance.py`, `tournament.py`, `monitor.py`, `replay.py`, `feedback.py` |
| Decision and existing-bot boundary | `intelligence/provider.py`, `runtime_contracts.py`, `decision.py`, `validation.py`, `operations.py`, `mt5_boundary.py`, `execution.py`, `management.py` |
| Live read-only collection | `observation.py`, `scheduler.py`, `live_runtime.py` |
| Application and evidence views | `runtime.py`, `research_orchestrator.py`, `dashboard.py` |

## Runtime boundary

`MT5ExecutionPort` is a protocol implemented by deployment code. The clean
build does not import MT5, credentials, direct broker clients, old gateways,
or old configuration. This preserves the required separation: intelligence
produces structured evidence and decisions; the existing bot remains the only
broker order layer.

## Validation

The VPS test command is:

```text
cd /opt/cipherfx_mt5
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=clean_build \
  /opt/cipherfx_mt5/.venv_mt5/bin/python -m pytest -q clean_build/tests
```

The latest VPS result is **367 passed**. These tests certify their individual
contracts only. They do not certify the complete 267-requirement build; the
master ledger remains incomplete until every requirement has all required
code, test, runtime, data, broker, and shadow evidence.

Latest verified live-research facts:

- all five configured HFM symbols produced fresh terminal-authored ticks;
- the ten-frame rolling snapshot path completed successfully;
- live intelligence receives a bounded persistent tick window rather than a
  single quote;
- the read-only collector produced only `NO_TRADE:NO_CERTIFIED_EDGE`;
- no broker event or order was created by the clean collector;
- the active compact historical library contains 3,723 clean states and paths;
- historical paths are bar-OHLC fidelity because the source export contains
  no historical executable bid/ask tick stream.
