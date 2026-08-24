# Clean Build Item 002 Audit

## Governing requirement

Item 2 from `spec_component_matrix.md` requires the existing CipherFX/MT5 bot
to receive approved structured trade decisions and remain the component that
places BUY and SELL orders. The clean intelligence system must not replace it.

## Status

**SCOPED PASS: clean decision-to-existing-bot execution handoff contract.**

**MASTER STATUS: NOT VERIFIED.** Item 2 is not an end-to-end document and
does not satisfy Component Matrix C002 until deployment and broker evidence
prove that a real approved instruction reaches the existing bot and produces
the corresponding MT5 result.

This is build and integration-contract evidence. No live broker order was sent
during validation, the production bot remained stopped, and Item 3 broker/MT5
interface validation is not claimed by this item.

## Runtime call path

`Market data -> IntelligenceOrchestrator -> immutable TradeDecision ->
validate_decision -> BotExecutionHandoff -> immutable BotInstruction ->
ExistingBotPort.submit -> BotExecutionResponse -> ExecutionReceipt ->
TradeOutcome -> EvidenceStore`

There is no alternate direct-broker call in the clean package.

## Line-by-line evidence

| Item 2 requirement | Result | Evidence |
|---|---|---|
| Receive an approved structured decision | PASS | `contracts.py::TradeDecision`, `execution.py::instruction_from_decision` |
| Preserve BUY/SELL direction and decision fields | PASS | Deterministic instruction conversion; parameterized BUY and SELL tests |
| Existing bot remains execution authority | SCOPED PASS | `execution.py::ExistingBotPort`; `runtime.py::CleanRuntime` injects `ExecutionPort` |
| Clean intelligence does not place broker orders | PASS | No MT5 import, `order_send`, direct broker port, or broker-order constructor in the runtime path |
| Do not rescore or reinterpret a decision | PASS | Handoff copies ID, edge, symbol, side, evidence, entry, stop, and target unchanged |
| NO_TRADE never reaches execution bot | PASS | Explicit `NO_TRADE` receipt and zero bot calls in tests |
| Invalid decisions never reach execution bot | PASS | Schema validation runs before instruction creation |
| One decision produces at most one bot submission | PASS | Atomic execution claim and stable SHA-256 request hash |
| Reused ID with changed content is rejected | PASS | `DECISION_ID_CONFLICT` test |
| Restart-safe duplicate prevention | PASS | SQLite `execution_handoffs` journal survives store reopen |
| Bot response belongs to submitted decision | PASS | Mismatched response ID becomes `BOT_RESPONSE_ID_MISMATCH` |
| Broker result is returned to the evidence path | PASS | Ticket, deal, fill, reason, and reference flow into receipt/outcome persistence |
| Submission failure has no fallback order path | PASS | Exception becomes one persisted `BOT_SUBMISSION_ERROR`; duplicate call returns the same receipt |
| Instruction contract is immutable and versioned | PASS | Frozen `BotInstruction`, `schema_version=1`, deterministic request hash |

## Validation evidence

VPS command:

```text
cd /opt/cipherfx_mt5
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=clean_build \
  /opt/cipherfx_mt5/.venv_mt5/bin/python -m pytest -q clean_build/tests
```

Latest complete clean-build suite: **367 tests passed**.

Additional required checks:

- Python `compileall`: PASS
- clean runtime direct-broker/MT5 import scan: PASS
- forbidden legacy import scan: PASS
- decision-to-bot BUY integration test: PASS
- decision-to-bot SELL integration test: PASS
- exactly-once and conflict tests: PASS
- durable restart idempotency test: PASS
- tick-to-intelligence-to-bot-to-persisted-outcome test: PASS
- production bot process remained stopped: PASS

## Item boundary

Item 2 does not claim that a live MT5 order was placed. Live broker interface,
symbol/tradability validation, and submit/modify/close behavior are Item 3.
Item 2 proves only that the clean intelligence path has one immutable,
idempotent handoff contract and cannot execute directly. It does not prove
that deployment code implements the port, that MT5 accepts an order, or that
the broker result returns through this path. Those claims require separate
runtime and broker evidence in the 267-requirement master ledger.
