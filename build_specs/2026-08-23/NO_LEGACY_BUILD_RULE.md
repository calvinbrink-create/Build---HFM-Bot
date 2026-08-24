# Clean-Build Boundary Rule

This rule is mandatory for every item built from the two authoritative scope
documents stored in this directory.

1. New items MUST be implemented in new, isolated modules.
2. Legacy runtime modules, old engines, fallback paths, hidden gates,
   deprecated configuration, and patched execution paths MUST NOT be imported,
   called, copied, or used as implementation inputs.
3. Existing legacy code may remain untouched for audit and rollback, but it
   must be unreachable from the new build.
4. A new item is not complete until static import checks, runtime call-path
   tests, and regression tests prove that the new path does not enter legacy
   code.
5. Existing code modified during earlier integration work is not considered a
   clean-build implementation and cannot be used as evidence of completion.
6. The bot remains stopped while a new item is being built or validated.
7. Work is item-gated. Every item must be audited line by line against its
   specified requirements and validated through code, runtime call-path, and
   regression evidence before it can be marked PASS.
8. A failed check is never reported as a completed status. It is repaired and
   rerun in the same item until every required check passes. The only
   completion message for an item is PASS; no progress or failure status
   messages are issued while the item is still being repaired.
9. The work does not stop after one item. It continues through every line of
   all three governing documents: the two preserved source documents and this
   build specification/index. The final completion message is allowed only
   after the complete end-to-end build has passed.
10. No hidden veto, sabotage path, undocumented cap, fallback strategy,
    duplicate authority, or unrelated code may be introduced. Every active
    path must be traceable to a governing requirement and covered by tests.
11. Profitability is never declared from an assumption or a passing unit test.
   It requires measured, cost-adjusted, out-of-sample and live validation.
   The bot remains stopped until those validations pass; no claim of
   guaranteed profit is permitted.
12. The complete Friday conversation is part of the governing scope. Every
    requirement discussed there must be represented in the appropriate clean
    folder and wired end to end. A narrowed subset, interface-only folder,
    sample implementation, or partial candle/pattern library is not a pass.
13. No patching is permitted for Friday-scope work. The implementation must
    be a complete clean folder build with its own contracts, tests, audit
    trail, and runtime path. No next item may begin until that folder passes
    every Friday requirement line by line.

The two source documents remain byte-for-byte preserved in
`spec_component_matrix.md` and `spec_edge_system_principles.md`. This file is
the explicit implementation boundary that governs how those documents are
realized on the VPS.
