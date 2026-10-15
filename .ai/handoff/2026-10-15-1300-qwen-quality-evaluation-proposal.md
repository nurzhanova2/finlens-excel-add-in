# Handoff: Qwen quality evaluation proposal

**Status:** proposed; not implemented and not externally validated.

## Context

FinLens now protects users from showing unexecuted calculation code or fabricated calculation results: generated Python is validated, executed in an isolated sandbox, retried at most twice, and its result is required before a computation-based narrative can be shown.

This does **not** yet prove that the production internal Qwen produces reliable code or sufficiently deep financial analysis. All automated behaviour checks used the explicit fake provider. Production Qwen, Excel Desktop on Windows, and screen-reader validation remain external acceptance work.

## Agreed next quality work

Create a separate **Quality & Qwen Evaluation** work item. It must be measured against representative, de-identified financial workbooks before claims about production quality are made.

### Evaluation set

- Prepare 10--20 de-identified workbooks and analyst questions; do not put raw confidential data in the repository.
- Cover P&L, balance-sheet, cash-flow, variance, margin, formula-review, missing-data, and chart questions.
- Define expected sources, whether a calculation is required, and a human-review rubric.
- Measure first-attempt code success, eventual success within three attempts, correct source citation, numeric correctness, response latency, and analyst-rated depth/usefulness.
- The harness may be prepared locally now, but it must run only when a production Qwen endpoint and approved de-identified fixtures are available.

### Candidate implementation improvements

1. Use low, explicitly configured temperature (target 0--0.2) for calculation/tool-call turns.
2. Improve `table()` numeric normalisation for common finance formats: spaces/non-breaking spaces, comma decimals, percentages, blanks, parentheses for negatives, and subtotal rows. Preserve original values and do not silently coerce ambiguous cells.
3. Return safe debugging context after a failed sandbox attempt: failing line/source excerpt, DataFrame column names, dtypes, shape, and available artifact IDs. Keep the existing restrictions: no paths, secrets, raw workbook values beyond the supplied artifact, or traceback leakage to UI.
4. Add one short, verified example of `frame()`, `table()`, `show_table()` and `show_chart()` to the calculation prompt.
5. Add financial-analysis guidance to the narrative prompt: explain drivers, trend/variance, margin, one-off effects, reconciliation checks, formula-versus-hardcode risks, assumptions, and next checks when relevant.
6. Add a quality gate that requests one expansion only when the response is materially thin. It must be context-aware: do not require two numeric facts for formula, small-range, or insufficient-data questions. A legitimate insufficient-data answer must remain possible.

## Non-goals

- Do not hard-code financial conclusions or analytic scenarios.
- Do not weaken sandbox policy, source attribution, session cleanup, or the read-only Excel guarantee.
- Do not claim quality improvements without running the production evaluation set.

## Tracker correction to make during the next review

`sprint-status.yaml` still lists Story 1.1 as `backlog`, although its implementation handoff exists at `2026-10-14-1507-story-1.1-verified-read-only-pane.md`. During the independent review, reconcile it to the appropriate review status; do not mark it done before the external acceptance checks are complete.

## References

- `api/app/adapters/qwen.py`
- `sandbox-image/finlens_runner.py`
- `.ai/handoff/2026-10-14-1745-epic-2-sandbox-retry-verifiable-results.md`
- `.ai/handoff/2026-10-15-1230-qwen-http-cancel-production-readiness-readme.md`
- `docs/acceptance/manual-acceptance.md`
