# CipherFX Edge System Build Specification

Status: source-of-truth capture only; no production code is changed by this file.
Captured: 2026-08-23
Production root: `/opt/cipherfx_mt5`

## Authoritative Documents

The complete supplied documents are preserved byte-for-byte beside this index:

- `spec_component_matrix.md`: 160 numbered requirements, from architecture separation through the final operating flow.
- `spec_edge_system_principles.md`: 42 operating principles defining the research, validation, governance, and execution separation.
- `FRIDAY_SCOPE_LEDGER.md`: recovered line-item build obligations from the
  complete Friday task, including the HFM research chain and clean runtime
  wiring.
- `NO_LEGACY_BUILD_RULE.md`: mandatory boundary prohibiting legacy code, old
  engines, fallbacks, hidden gates, and patched runtime paths from any new
  implementation item.

SHA-256:

- `spec_component_matrix.md`: `77a3fa8b0e786b426d1b13c15f11f964099f2f6d89bc6fdb99758285a06393cc`
- `spec_edge_system_principles.md`: `b1c6f6944d45ce97bf747ac1456f188eddb87290fc7f278b110194fe3eb1478f`

## Non-negotiable operating contract

1. Market data is collected from MT5 and retained as raw ticks, validated candles, market states, charts, features, and outcomes.
2. Research/ChatGPT is an intelligence and edge-discovery layer. It proposes structured, evidence-backed decisions; it does not place broker orders.
3. The existing CipherFX/MT5 bot is the sole broker execution layer. It performs deterministic broker, margin, spread, slippage, volume, duplicate, and risk checks, then submits, modifies, and closes orders.
4. Every proposal must be traceable to source data, feature state, chart/state evidence, edge version, validation result, and broker outcome.
5. No score, indicator, chart pattern, memory result, or language-model opinion is sufficient without measured outcomes, sample size, costs, uncertainty, and out-of-sample validation.
6. The research dataset must include winners, losers, rejected setups, missed opportunities, ordinary no-trade states, failed patterns, and counterfactual outcomes.
7. Candidate edges must pass instrument-, session-, regime-, timeframe-, transaction-cost-, executable-price-, purged-walk-forward-, out-of-sample-, and shadow-forward validation before promotion.
8. Every experiment is versioned. Failed hypotheses remain in a strategy graveyard; they are never silently reused as active logic.
9. Online learning may update research statistics and future candidates only. It may not rewrite an approved live proposal or bypass execution controls.
10. The production sequence is:

    `MARKET -> DATA LIBRARY -> CHARTS -> INTELLIGENCE -> HISTORICAL SIMILARITY -> EDGE STATISTICS -> VALIDATION -> STRUCTURED DECISION -> BOT RISK/EXECUTION CHECKS -> MT5 ORDER -> POSITION MANAGEMENT -> OUTCOME FEEDBACK`

## Audit interpretation

The attached requirements are a build specification, not evidence that the implementation exists. A requirement is PASS only when an active runtime call path and a passing validation prove it. Comments, backup files, research JSON, or an unused module do not count as active implementation evidence.

The clean build parses all 160 component requirements, all 42 operating
principles, and all 65 recovered Friday obligations into a compliance ledger.
A module or interface cannot mark an item PASS. Each item declares the code,
test, runtime, data, broker, or shadow evidence required for completion.

## Current VPS baseline at capture

- Git HEAD: `57f432cbc4cccfa26ce3c299ac69ce994a946e8b`
- Bot process: **not running**.
- MT5 terminal: running under the HFM prefix; this is not proof that the bot is running.
- Active platform route: `MarketDataEngine -> LearningEngine -> asset engine -> TradeProposal -> ExecutionEngine`.
- Active market snapshot: H4, H1, M15, M5 only.
- Active strategy dispatcher: existing Forex, Indices, and Metals modules; not the complete 160-item edge-discovery system.
- Active ChatGPT/OpenAI research gateway: not found in the production call path.
- Existing `pattern_memory` is a limited chart-shape evidence store; it is not the required whole-market state, analogue, outcome, or governance system.

## Initial compliance result

**FAIL: the supplied end-state is not fully built on the VPS.** The existing execution/feedback skeleton is present, but the research, validation, governance, data-library, and structured intelligence layers are incomplete. The detailed PASS/PARTIAL/FAIL reconciliation belongs in the accompanying audit report and must be rerun after each implementation milestone.
