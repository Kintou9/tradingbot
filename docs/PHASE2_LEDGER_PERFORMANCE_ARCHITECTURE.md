# Phase 2, Step 1: ledger performance optimization architecture

Status: proposed architecture, awaiting independent review. Investigation date: 2026-10-11. This document authorizes no implementation, commit, merge, deployment, provider access, or trading operation.

## 1. Executive summary

The primary ledger's active-order path is quadratic in journal length. Every published transition calls `assert_invariants`, and that function starts at initial cash and replays every terminal outcome. `PortfolioState.validate` also scans all prior outcomes, while adding an outcome copies an immutable tuple. These costs occur for opening and closing marks, pending batches, fills, rejections, expiries, construction, and final result creation.

Measured offline evidence distinguishes this from ordinary marking cost. A one-symbol, zero-order workload grew from 0.0094 seconds at 100 sessions to 0.154 seconds at 1,600 sessions. A one-symbol workload with one terminal order per session grew from 0.0686 to 28.38 seconds; 25.98 seconds, or 91.5%, were measured inside journal-backed invariant validation at 1,600 sessions. Five-symbol contention took 33.05 seconds at 800 sessions, of which 29.36 seconds, or 88.8%, were validation.

Use a two-level design:

1. Validate each transition against its immediately preceding trusted state and the exact submitted intent/fill. Maintain unique-order indexes and cumulative accounting totals incrementally in the ledger. This is bounded by the affected orders and current holdings.
2. At finalization, run one mandatory, independent full-journal reconciliation from initial cash. It must reconstruct terminal cash, holdings, cost bases, realized P&L, fees, slippage, and terminal order status, verify the complete snapshot history, and issue a deterministic audit receipt. A failed audit produces no `SimulationResult` and therefore no report.

`summarize_primary` remains an independent report-time reconstruction. It must require evidence that final ledger reconciliation succeeded and must continue to reject altered results itself. The journal remains immutable evidence; only its runtime storage changes from repeatedly copied/scanned state to append-only ledger-owned records.

## 2. Current architecture and call graph

The coupled path is:

```text
CLI / window runner
  -> run_primary_strategy
     -> PrimaryLedger.__init__
        -> PriceBook
        -> assert_invariants(empty state)
     -> for each session
        -> open_session
           -> mark_portfolio
              -> PortfolioState.validate(previous)
              -> copy every held Position and pending mapping
              -> PortfolioState.validate(marked)
           -> _publish(opening_marks)
              -> assert_invariants(all prior outcomes)
              -> construct PortfolioSnapshot and LedgerEvent
           -> for each pending sell, then buy
              -> _propose / size_entry
              -> apply_fill or _nonfill
                 -> _advance (copy positions/pending)
                 -> append by copying outcomes tuple
                 -> _publish
                    -> assert_invariants(all prior outcomes)
                    -> construct snapshot/event
        -> close_session
           -> mark_portfolio -> two local state validations
           -> _publish(closing_marks) -> full replay
        -> evaluate_close for every symbol
        -> submit_close
           -> copy submitted registry and state mappings
           -> validate batch
           -> _publish(pending_update) -> full replay
     -> finish
        -> one _nonfill/_publish per terminal pending intent
        -> result
           -> assert_invariants(all outcomes)
           -> scan fills and match closed trades
           -> SimulationResult.__post_init__ scans IDs and sorts history times
  -> summarize_primary
     -> independently match intents/outcomes/fills
     -> replay fills while visiting every historical snapshot
     -> reconstruct cash, holdings, gross/net P&L and costs
     -> compute daily curves, metrics, report records and fingerprints
```

`run_window` slices immutable prepared data, runs that complete path with fresh state and calls `summarize_primary`. `run_primary_windows` repeats it for every requested window. `build_multi_window_report` then aggregates already reconciled reports and fingerprints the aggregate. Windows therefore multiply execution and audit cost, but do not cause state leakage.

Current validation and copying sites:

| Site | Work | Frequency | Classification |
| --- | --- | --- | --- |
| `primary_ledger.assert_invariants` | Rebuilds cash, positions, gross/net P&L, fees and slippage from every outcome; checks intent/fill/book/terminal state | Every `_publish`, initialization and `result` | Financially necessary as a full audit, redundantly repeated |
| `PortfolioState.validate` | Local numeric/equity checks plus scans all outcomes for unique IDs and future times | Directly and through marking/views/full audit | Local checks necessary; journal scans redundant per transition |
| `mark_portfolio` | Validates before/after, copies every position and pending map, recomputes exposure | Open and close | Necessary phase marking; bounded by open positions |
| `_advance` | Copies positions, marks and pending | Each order transition/submission | Supports atomic immutable publication; bounded by current holdings |
| outcome tuple concatenation | Copies all prior outcomes | Each terminal outcome | Immutability goal is valid; repeated tuple copying is unnecessary |
| `_publish` | Sorts positions/pending and stores a full immutable snapshot | Every transition | Externally observable history; retain unless separately versioned |
| `result` / `SimulationResult` | Rescans outcomes/fills/history | Once | Necessary final construction; some checks can share audit indexes |
| `summarize_primary` | Independent fill and snapshot reconciliation plus serialization preparation | Once per run/window | Necessary independent audit/report boundary |
| report serializers | Canonical traversal, JSON conversion and hashing | Once per emitted report | Necessary for deterministic output |

## 3. Reproduced baseline measurements

Measurements used CPython 3.12.13 and the repository virtual environment. Inputs were deterministic offline daily bars made in memory; no provider, broker, database, network, or LLM was used. Preparation used `prepare_primary_data`. Ledger timing started with `PrimaryLedger` construction and ended after `finish`. Reporting timing separately called `summarize_primary`. A runtime wrapper measured inclusive time in `assert_invariants`. Normal measurements used three fresh-process repetitions and report medians; the 800-session contention case used one run because it was already expensive. First-process indicator import/initialization made preparation maxima noisy, so preparation medians are reported and are not used to diagnose ledger complexity.

Workloads:

- **A, minimal activity:** one symbol, one open/close/pending publication per session, no orders or fills.
- **B, frequent accepted trading:** one symbol alternates full buy and sell intents; one terminal outcome per session after the first and one tail expiry.
- **C, shared-capital contention:** five symbols submit an intent each close with 100% remaining-cash allocation. Alphabetical sell-before-buy execution produces accepted fills plus cash rejections. This is a synthetic ledger stress workload, not a strategy result.

### Scenario A: minimal activity

| Sessions | Symbols | Fills / rejected / outcomes | History | Ledger s | Validation s | Report s | Ledger ratio |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 100 | 1 | 0 / 0 / 0 | 300 | 0.0094 | 0.0021 | 0.0130 | — |
| 200 | 1 | 0 / 0 / 0 | 600 | 0.0205 | 0.0045 | 0.0217 | 2.19x |
| 400 | 1 | 0 / 0 / 0 | 1,200 | 0.0398 | 0.0087 | 0.0327 | 1.94x |
| 800 | 1 | 0 / 0 / 0 | 2,400 | 0.0756 | 0.0169 | 0.0595 | 1.90x |
| 1,600 | 1 | 0 / 0 / 0 | 4,800 | 0.1537 | 0.0339 | 0.1282 | 2.03x |

Median preparation times were 0.0176, 0.0259, 0.0377, 0.0674, and 0.1454 seconds. Ledger ranges at 1,600 sessions were 0.1450–0.1644 seconds.

### Scenario B: frequent accepted trading

| Sessions | Fills / rejected / expired / outcomes | History | Ledger s | Validation s (% ledger) | Report s | Ledger ratio |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 100 | 99 / 0 / 1 / 100 | 400 | 0.0686 | 0.0440 (64.1%) | 0.0330 | — |
| 200 | 199 / 0 / 1 / 200 | 800 | 0.2682 | 0.1959 (73.0%) | 0.0615 | 3.91x |
| 400 | 399 / 0 / 1 / 400 | 1,600 | 1.0735 | 0.8668 (80.7%) | 0.1271 | 4.00x |
| 800 | 799 / 0 / 1 / 800 | 3,200 | 5.0672 | 4.4110 (87.1%) | 0.2034 | 4.72x |
| 1,600 | 1,599 / 0 / 1 / 1,600 | 6,400 | 28.3794 | 25.9813 (91.5%) | 0.4742 | 5.60x |

Ledger ranges were 0.0681–0.0726, 0.2604–0.2801, 1.0695–1.1359, 4.9727–5.4886, and 27.8161–28.4333 seconds. Median preparation stayed between 0.0175 and 0.1471 seconds.

### Scenario C: five-symbol contention

| Sessions | Fills / rejected / expired / outcomes | History | Ledger s | Validation s (% ledger) | Report s | Ledger ratio |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 100 | 197 / 298 / 5 / 500 | 800 | 0.3801 | 0.3008 (79.1%) | 0.0848 | — |
| 200 | 397 / 598 / 5 / 1,000 | 1,600 | 1.3440 | 1.1034 (82.1%) | 0.1650 | 3.54x |
| 400 | 797 / 1,198 / 5 / 2,000 | 3,200 | 8.8616 | 7.5761 (85.5%) | 0.5185 | 6.59x |
| 800 | 1,597 / 2,398 / 5 / 4,000 | 6,400 | 33.0539 | 29.3577 (88.8%) | 0.7334 | 3.73x |
| 1,600 | not run | — | — | — | — | — |

The 100–400 rows are medians of three repetitions. Their ledger ranges were 0.3503–0.5090, 1.3294–1.3820, and 7.0137–10.1248 seconds. The 800 row is one measurement. Preparation medians were 0.0779, 0.1082, 0.3001, and 1.7875 seconds. A 1,600-session contention run was stopped before launch as impractical for this architecture task: quadratic extrapolation from the measured 800 result suggests about 132 seconds for one run, but that is explicitly an unmeasured projection.

A `cProfile` run for C/200 added profiler overhead and took 6.51 seconds, so it is not comparable to wall-clock medians. It recorded 7.75 million calls. `_publish` consumed 5.47 seconds cumulatively; `assert_invariants` consumed 5.27 seconds over 1,602 calls, including 2.40 seconds in its own body. `PortfolioState.validate` consumed 1.52 seconds over 2,802 calls. Price lookups, event comparisons, and full-outcome generator scans dominated the remaining called work. The raw benchmark script and profile were kept in `/private/tmp`, outside the repository.

## 4. Root-cause analysis

Let `T` be sessions, `N` symbols, `J` terminal order outcomes, `E` published transitions, and `P` simultaneously held positions. In active workloads, `E` and `J` grow with `T×N`.

At transition `e`, `assert_invariants` scans all outcomes accumulated so far. Total replay work is `sum(j_e)`, or `O(E×J)` and `O(J²)` when both grow together. It also performs price-book lookups and intent matching for every historical fill on every replay. `PortfolioState.validate` independently constructs ID collections and checks every historical outcome's time, adding another `O(E×J)` component. Appending with `candidate.outcomes += (...)` copies a length-`J` tuple, adding `O(J²)` allocation/copy work across a run.

The zero-order workload has `J=0`, so it remains approximately linear despite 4,802 validation calls at 1,600 sessions. The active workload has 6,402 calls and 1,600 outcomes, and its near-fourfold time growth for early doublings is the expected quadratic signature. The worsening ratio at larger sizes is consistent with allocation and cache effects; no claim depends on an exact exponent.

Position marking and snapshot construction cost `O(P)` per event. They are visible but are not the primary cause in these workloads. Reporting is approximately `O(H×P + F + D)` where `H` is stored snapshots and `F` fills; its measured growth is far below the ledger's active-journal growth. Strategy evaluation is `O(T×N)` plus evaluator work. It was deliberately excluded from the scripted ledger stress timing; the quiet path's low ledger cost and profiler attribution provide the bottleneck evidence without attributing signal logic to the journal.

## 5. Complexity analysis

| Operation | Current | Proposed |
| --- | --- | --- |
| Local mark transition | `O(P + J)` because publication replays journal | `O(P)` |
| Fill/rejection/expiry | `O(P + J)` plus tuple copy `O(J)` | `O(P + B)` for affected batch/order; amortized `O(1)` journal append |
| Pending batch | `O(P + B + J)` | `O(P + B)` |
| Full execution | `O(T×N + E×P + E×J + J² tuple copies)` | `O(T×N + E×P + J)` before snapshots/reporting |
| Mandatory final audit | Repeated throughout, plus final | One `O(J + H×P)` reconciliation |
| Reporting | `O(H×P + F + decisions)` | Same |
| Journal memory | Immutable outcomes repeatedly copied transiently; final `O(J)` | Append-only internal `O(J)`, immutable result tuple `O(J)` once |
| Snapshot memory | `O(H×P)` | Unchanged for compatibility |

`P` is naturally bounded by the symbol universe in v1. The proposal does not claim constant time when many positions must be marked or serialized.

## 6. Comparison of optimization approaches

| Option | Complexity | Correctness and drift risk | Compatibility/testability | Auditability | Decision |
| --- | --- | --- | --- | --- | --- |
| A. Incremental invariant maintenance only | Execution can approach linear | Duplicated cumulative fields can drift; without mandatory replay, correlated bugs can bless bad state | Moderate implementation surface; corruption tests essential | Weak if full replay becomes optional | Use its local checks and indexes, but not alone |
| B. Full reconciliation at checkpoints | End-only is linear; every session remains `O(T×J)` and therefore quadratic; fixed every `K` events is `O(J²/K)`; geometric checkpoints are `O(J log J)` | Earlier detection than end-only, but no benefit sufficient to justify repeated complete replay | More policy and timing branches | Strong at each checkpoint | Support explicitly requested diagnostic checkpoints later; do not use session replay by default |
| C. Efficient execution plus mandatory audit-time verification | `O(E×P + J)` execution, one full audit, independent report audit | Local corruption can survive until finalization, but cannot produce a result/report; immediate structural errors still fail | Clean boundary and independently testable pure auditor | Strong: journal remains authoritative evidence | **Recommended** |
| D. Persistent tree/prefix hashes with proof nodes | Potential logarithmic verification | Considerably more implementation and proof complexity; accounting bugs can be embedded in the tree | High compatibility risk | Strong only with carefully specified proofs | Reject for this phase |

The recommendation combines bounded incremental validation with Option C. It does not replace audit arithmetic with trusted running totals. Running totals support fast execution; the auditor independently rebuilds them from immutable evidence.

## 7. Recommended design

### Execution layer

Keep `PrimaryLedger` as the sole mutation authority. Add private append-only `_outcomes` and `_submitted` registries plus `_terminal_order_ids`. Do not store or concatenate the growing outcome tuple in each internal runtime state. Expose immutable tuple copies through `ledger.state` and `SimulationResult` only at explicit observation/finalization boundaries. Existing public outcomes, fills, statuses, and ordering remain identical.

Split validation into three contracts:

1. `validate_snapshot_local(state)` checks finite/nonnegative values, position/pending consistency, `equity == cash + exposure`, and `equity - initial_cash == realized_net + unrealized_net`. It may inspect current `P`, never prior `J`.
2. `validate_transition(previous, candidate, transition, indexes, book, config)` checks the exact delta: event time increases; a nonfill changes no economics; a buy debit matches price, fees, slippage, allocation, cash, and one new position; a sell references and removes the exact position and credits correct cash/P&L; pending updates contain unique next-session intents; marks use only the current price or prior mark. It updates ledger indexes only after every check succeeds.
3. `reconcile_journal(...)` is a pure independent auditor. Starting from initial cash, it consumes submitted intents and terminal outcomes exactly once, reconstructs holdings and accounting, walks snapshots chronologically, and compares the reconstructed terminal state and history. It must not call `validate_transition`, use incremental totals as its arithmetic oracle, or mutate ledger state.

Append a transition atomically only after local checks succeed. If list append or snapshot construction raises, do not publish candidate state or indexes. Preserve sells-before-buys alphabetical order and re-read cash after every actual fill.

### Finalization and audit receipt

`finish` expires terminal intents using the same bounded transition checks, freezes journal collections once, and calls `reconcile_journal` exactly once. Success returns an immutable `JournalAuditReceipt` containing auditor version, dataset hash, canonical configuration digest, submitted/outcome/fill/snapshot counts, terminal state digest, journal digest, and `passed=True`. Digests identify the audited values; they are not a substitute for arithmetic checks.

`SimulationResult` carries the receipt as internal audit evidence. `summarize_primary` must verify the receipt matches the result's digests before performing its existing independent reconstruction. The report may add explicit performance/audit metadata only under a deliberately versioned schema decision; it must not silently change financial fields. Because current report fingerprints include source code hashes, implementation changes legitimately change report fingerprints even when financial records are identical.

### Diagnostic checkpoints

Permit an internal/test-only `audit_checkpoint()` that runs the same pure reconciler against a frozen prefix and returns a receipt without altering state. Do not call it every session in production paths. A future CLI flag could request checkpoints, but adding that flag is outside this implementation unless separately approved.

## 8. Exact proposed files and functions to change

### `backtesting/primary_state.py`

- Add frozen `JournalAuditReceipt` with deterministic validation.
- Add `audit_receipt` to `SimulationResult`, with a compatibility-conscious default only during migration. Final coupled results must require a passing receipt.
- Refactor `PortfolioState.validate` into local snapshot checks. Remove full-outcome uniqueness/future scans from the hot internal state path; preserve equivalent full checks in the auditor.
- Do not change financial record schemas, enum values, quantity, fee, slippage, or mark semantics.

### `backtesting/primary_ledger.py`

- Rename current `assert_invariants` implementation to a pure `reconcile_journal` and extend it to verify snapshot history and emit a receipt.
- Keep an `assert_invariants` compatibility wrapper if tests or callers import it; document that it is a full explicit audit, not a hot-path validator.
- Add `validate_transition` and small side-specific local validators.
- Change `PrimaryLedger` to own append-only outcome storage and unique-ID indexes; avoid outcome tuple concatenation during execution.
- Change `_publish` to validate only the proposed local transition, construct the same snapshot/event, and publish atomically.
- Change `finish`/`result` so a mandatory audit occurs once and a failed audit prevents result construction.
- Preserve `size_entry`, order sorting, fill proposal arithmetic, missing-bar behavior, terminal expiration, event sequence, and method signatures.

### `backtesting/primary_marking.py`

- Route before/after checks through the local snapshot validator. Preserve exact open/close lookup, stale-mark, copying, and equity behavior.

### `backtesting/primary_reporting.py`

- Verify the final audit receipt against immutable result digests before reporting.
- Retain the independent reconciliation loop and all existing metric definitions.
- Update the existing `invariant_cost` statement to describe the optimized architecture. Treat changed code hashes/report fingerprints as expected code-identity changes.

### `backtesting/primary_windows.py` and `primary_window_reporting.py`

- No accounting behavior change. Ensure every window result/report required and verified its own receipt. Update the obsolete quadratic-scaling limitation after measured implementation evidence exists.

### Tests

- Extend `tests/test_primary_ledger.py`, `test_primary_marking.py`, `test_primary_reporting.py`, `test_primary_windows.py`, and `test_primary_window_reporting.py` for transition parity, receipt enforcement, independent corruption detection, and window isolation.
- Add `tests/test_primary_ledger_performance.py` for deterministic operation-count/scaling assertions. It should not impose fragile ordinary-CI wall-clock limits.

The CLIs need no argument change. They receive audited results through existing strategy/window calls.

## 9. Preserved financial invariants

Every accepted buy must debit `quantity × slipped_price + fee`, stay within current cash and the cost-inclusive allocation budget, create exactly one positive long position, and apply fees/slippage once. Every accepted sell must reference the held entry ID, liquidate the full quantity, credit `quantity × slipped_price - fee`, and update realized gross/net P&L once. Rejections and expiries have zero cash delta and never create or remove a position.

At every published state:

- cash is finite and nonnegative within the existing tolerance;
- exposure is the sum of quantity times current/stale defensible marks;
- equity equals cash plus exposure;
- equity less starting cash equals realized net plus unrealized net;
- gross P&L less fees and slippage equals net P&L;
- cumulative fees/slippage never decrease;
- pending IDs and symbols are unique and consistent with current holdings;
- terminal outcomes are unique and cannot later fill;
- position quantity, cost basis, entry price, stops, targets and provenance derive from the actual accepted entry;
- order chronology is signal close, later eligible open, with sells before buys and alphabetical order;
- no current close or future cash can affect an earlier open;
- terminal intents expire with the existing reason;
- each independent window starts with fresh cash/state and uses strict observed inclusive boundaries.

Neither the local validator nor the auditor may clamp cash, repair records, tolerate economically meaningful differences, or alter a failed state.

## 10. Independent reconciliation mechanism

The pure auditor accepts only frozen inputs: initial config, price book/prepared data identity, ordered submitted intents, ordered outcomes/fills, ordered snapshots, and claimed terminal state. It builds its own sets/maps and totals.

It verifies one pending-or-terminal disposition per submitted ID, strict outcome chronology, intent/fill correspondence, next-session eligibility, book open, execution price, fee, slippage and cash delta. It reconstructs cash and holdings event by event, including position metadata and cost basis; compares reconstructed values to each stored snapshot; then checks terminal holdings, realized gross/net P&L, all costs, and total economics. It hashes canonical audited inputs only after these checks pass.

The audit is mandatory at finalization. Report generation verifies its receipt and independently repeats report-oriented fill/snapshot reconciliation. Explicit diagnostic checkpoints use the same auditor against a prefix. No production path may construct an apparently successful coupled result without the final receipt.

## 11. Failure behavior

Structural problems detected by a local transition check raise before publication and leave state, journal, indexes, and sequence unchanged. Arithmetic or journal discrepancies found during final audit raise a dedicated `LedgerReconciliationError`; `finish` returns no result. Reporting raises `ReportingError` for a missing/mismatched receipt or its own reconciliation failure. Window orchestration follows its existing fail-closed behavior and must not count a failed window as completed or emit a valid aggregate fingerprint.

Errors must identify the order/event and invariant category without including credentials, provider metadata, or local input paths. There is no fallback to unchecked results, no warning-only mode, and no automatic repair.

## 12. Compatibility contract

Preserve method signatures where practical; intent/fill/outcome/position schemas; enum values and reason codes; order IDs; event and history ordering; sell-before-buy alphabetical execution; current-cash allocation; fees/slippage; phase marks; terminal positions; strict and legacy valuation behavior; observed-session windows; single/multi-window report financial fields; and all CLI arguments.

For identical inputs, all financial records, decisions, outcomes, histories, metrics, serialization ordering, and error categories must match the Phase 1.5 baseline. Source hashes in reports will change and therefore report fingerprints may change legitimately. An audit receipt or performance metadata would also change the fingerprint if exported; that requires an explicit schema decision and golden-output update. Do not suppress code-identity changes or reuse an old fingerprint.

The append-only runtime journal remains private. Public `ledger.state.outcomes`, `result.outcomes`, `events`, and submitted mappings remain immutable observations with the same values. Defensive copying must remain at public boundaries.

## 13. Adversarial test plan

### Differential financial parity

Freeze Phase 1.5 expected journals and independent arithmetic oracles for accepted buys/sells, rejected buys, impossible/orphan sells, partial cash, simultaneous competing buys, fees/slippage, open and closed positions, zero trades, and final pending expiry. Compare full ordered decisions, outcomes, fills, snapshots, terminal positions, closed trades and financial report fields. Do not compute expected values with the optimized helpers.

### Historical parity

Retain and extend missing-next-open expiry, stale-mark buy rejection, valid stale-position sale, final-session signal expiry, future-price prefix invariance, sell-before-buy funding, reverse-input determinism, strict valuation rejection, legacy opt-in, baseline comparison, independent window reset, and non-observed boundary rejection tests.

### Immediate corruption detection

Inject a candidate transition with wrong cash, fill quantity, cost basis, fee, slippage, position ID, pending ID, mark source, sequence, or allocation. The local validator must reject it atomically. Attempt duplicate IDs and post-terminal reuse; the index check must reject immediately.

### Final-audit corruption detection

After locally valid events, deliberately alter one frozen outcome cash delta/fill journal record, delete or reorder an outcome, alter cumulative realized P&L, terminal quantity/cost basis, a historical snapshot, journal digest, or receipt count. The independent final auditor must fail. Alter a result after receipt creation; reporting must reject the receipt mismatch and must also retain its independent arithmetic rejection cases.

### Performance regression

Use operation-count tests: monkeypatch the full auditor and assert exactly one final call per simulation/window; assert local transition validation never iterates a sentinel historical sequence; verify outcome appends do not copy a growing tuple; confirm final audit visits each outcome/snapshot once. Keep a manual benchmark matching scenarios A/B/C at 100–1,600 sessions. Ordinary CI should test doubling ratios or audited-item counts on moderate sizes, with generous environment-independent bounds. A scheduled benchmark may alert on wall time, but should not gate correctness on a single absolute duration.

## 14. Performance acceptance criteria

Implementation acceptance requires:

1. Scenario B reaches 1,600 sessions and scenario C reaches at least 800 sessions with exactly one mandatory full audit per run.
2. Instrumented total outcome visits in execution local validators are `O(J)`, excluding the one final audit and independent report audit; no transition scans all prior outcomes.
3. Outcome storage has amortized constant-time append and freezes once for the result.
4. For B at 200/400/800/1,600, doubling ledger time should be materially below the baseline's approximately 4–5.6x pattern. The target is no worse than 2.5x for consecutive doublings after warm-up on the same machine, assessed by medians of at least three runs. This is a review benchmark, not a portable CI assertion.
5. At 1,600 B sessions, target ledger runtime is under 2.0 seconds on the investigation machine. This is a projection-based engineering target, not a measured optimized result or correctness requirement.
6. At 800 C sessions, target ledger runtime is under 5.0 seconds on the investigation machine. This is likewise a target; final documentation must replace it with measured evidence.
7. Reporting may remain linear and must not be weakened merely to meet a speed target.
8. Every existing and new correctness test passes; any financial or historical difference fails acceptance regardless of speed.

The targets allow substantial overhead above the measured quiet path and report costs. They are intentionally conservative and must be revisited with implementation measurements.

## 15. Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Incremental totals drift with journal evidence | Mandatory independent final reconstruction; report performs another independent reconciliation |
| Internal mutable list leaks to callers | Freeze to tuples at state/result boundaries; retain mapping proxies and defensive copies |
| Atomicity breaks when indexes update early | Validate candidate first; commit state, journal and indexes in one small publication section; fault-injection tests |
| Receipt falsely blesses altered records | Receipt hashes canonical config/data/journal/history/terminal state; reporting recomputes and compares digests |
| Shared helper creates correlated validator/auditor bug | Keep transition arithmetic and auditor loops separate; use analytical test oracles |
| End-only audit delays detection | Immediate local structural/economic checks remain; explicit diagnostic checkpoint available |
| Snapshot cost dominates after replay removal | Measure separately; retain compatibility first, propose snapshot thinning only as a later schema change |
| Window runs repeat audit/report work | Required independent evidence per window; optimize only after profiling post-change |
| Code hashes alter fingerprints | Document expected identity change; compare financial payloads separately in parity tests |
| Profiler distorts absolute timing | Use `perf_counter` fresh-process medians for baseline; use profiler only for attribution |

## 16. Implementation sequence

1. Add frozen audit receipt/error types and pure canonical digest helpers without changing execution.
2. Extract the current replay into `reconcile_journal`; add snapshot-history checks and golden corruption tests while the old hot-path calls still exist.
3. Add local snapshot and transition validators with analytical tests for every event type.
4. Introduce ledger-owned append-only outcome storage and ID indexes. Preserve public immutable observations and run differential journal tests after each change.
5. Replace `_publish` full replay with local validation. Keep mandatory full reconciliation in `finish`, attach receipt, and make result construction fail closed.
6. Require and verify the receipt in reporting while retaining its independent reconciliation. Update window enforcement and obsolete performance wording.
7. Run all Phase 1/1.5 suites, corruption tests, deterministic serialization comparisons, compilation and scope checks.
8. Re-run the exact benchmark matrix, record medians/ranges/profile attribution, and compare operation counts and financial outputs. Stop if parity differs; do not tune accounting behavior.

Each unit should remain reviewable and reversible. No CLI, signal, valuation, broker, provider, database, or live-execution changes belong in these commits.

## 17. Open questions and architectural blockers

No blocker prevents implementation after independent approval. Three decisions should be made explicitly during review:

1. Whether `JournalAuditReceipt` remains internal to `SimulationResult` or is exported in a new report schema. Internal-only minimizes schema churn; exporting improves downstream proof visibility but changes fingerprints and requires versioning.
2. Whether `ledger.state` must materialize all historical outcomes on every property access for strict compatibility. Tests use it; production callers are unknown. The safe default is yes, with documentation that observation is `O(J)`, while the engine hot path never calls it.
3. Whether stored transition-level history must remain complete indefinitely. This proposal preserves it. A close-only or delta-compressed history could reduce memory/report cost later, but it is a separate compatibility and audit design.

The measured bottleneck does not require changing trading policy, accounting rules, windows, report metrics, or historical data. The architecture is ready for independent read-only review before implementation.
