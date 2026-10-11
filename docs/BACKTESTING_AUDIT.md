# Backtesting audit and merge readiness

Final review: 2026-10-10. Scope: Phase 1 historical simulation only.

## Decision and provenance

Ready for a controlled local commit and bounded historical-simulation merge review. No outstanding blocker was identified within this scope. This does not certify production parity, profitability, or unattended live trading. The primary runner's precomputed-signal limitations are explicitly accepted as boundaries of this phase, not claimed to be solved.

The original [Phase 1 verification report](../PHASE1_VERIFICATION.md) is preserved byte-for-byte as a historical record. Its 209-test result was independently reproduced during this review: 209 passed, 0 failed, 0 skipped, 258 warnings, 15.99 seconds. Its earlier 176/152 baseline results and initial nine failing adversarial cases describe the prior audit; they were not rerun against reconstructed old versions in this final review.

**Correction to the original report:** swing sizing is fractional at the existing sizer's default 0.0001-share increment, not whole-share-only. The module documentation is corrected; no sizing policy was changed. A new analytical regression verifies 14.2857 shares for a $100 risk budget and a $7 stop distance. The unsupported-sentiment message now describes this harness's missing input rather than asserting unverified current provider-tier capabilities.

## Git review and intended scope

Before review, active branch was `backtest-accuracy-phase1`; HEAD, local main, and merge base were all `a83bececd80bfbaeab185222a6897f1f7acf1492`. Commit history ends with the existing LangGraph documentation work. All Phase 1 changes remained uncommitted. `git ls-remote origin refs/heads/main` returned the same hash: remote main had not diverged at inspection time. This check did not fetch or update local refs. The first sandboxed attempt failed DNS resolution; the approved network retry succeeded.

The staged diff was inspected; its exact paths and blob contents were checked against the reviewed files. No unrelated path was staged.

Exact commit allowlist (12 files):

- `backtesting/backtest_runner.py`
- `backtesting/swing_backtest.py`
- `backtesting/run_watchlist_backtest.py`
- `backtesting/run_swing_backtest.py`
- `backtesting/metrics.py`
- `tests/test_backtest_runner_execution.py`
- `tests/test_swing_backtest_sizing.py`
- `tests/test_backtest_regression_guards.py`
- `tests/test_phase1_audit.py`
- `tests/test_phase1_final_review.py`
- `PHASE1_VERIFICATION.md`
- `docs/BACKTESTING_AUDIT.md`

Four tracked files are modified; eight files are new. No files are deleted. `PROJECT_HISTORY.md` is unrelated, preserved, and excluded. `.env`, `.env.example`, and the original verification report are preserved using before/after SHA-256 checks. No environment files, datasets, generated logs, bytecode, or temporary backups belong to the commit. Review of candidate contents and credential-pattern checks found no credential material; references to configuration variable names are intentional.

Only backtesting, tests, and documentation are included. Alpaca submission behavior, LangGraph, Claude prompts/policies, database schemas, authentication, DCF formulas, trading thresholds, dependencies, and the separate options bot are unchanged. No broker API, paid research, or live market-data command was run, and no real orders were submitted by this review. Execution tests use fake brokers and mocked services; broker account activity was not queried.

## Accounting and execution model

The swing ledger processes the union of available dates in order. Before entries, existing holdings are valued at that session's open or their last known mark. Equity equals cash plus marked holdings. Candidates compete alphabetically for one cash pool and one outstanding planned-risk pool. The sizer uses current equity, stop distance, and remaining aggregate risk capacity; unaffordable positions are rejected rather than borrowing or silently shrinking to cash.

For quantity q, reference entry e, slippage s, and fee rate f:

- Entry debit = q × e × (1 + s) × (1 + f).
- Exit credit = q × exit_price × (1 − s) × (1 − f).
- Realized net P&L = exit credit − entry debit.
- Planned risk reserved = q × (e − stop).
- End-of-session equity = remaining cash + sum(q × closing mark).

Cash and risk are released on the actual exit date, never while evaluating a future path. Entries precede same-session exits, so unknown intraday proceeds cannot fund an entry. Aggregate risk is checked when admitting entries; existing positions are not forcibly liquidated when falling equity reduces the percentage budget. Costs and gaps can exceed planned stop risk.

Setups use a completed bar; confirmations execute on the next available session, at the breakout trigger or gap-aware open. Existing exit logic resolves gaps at the open, stop/target ambiguity adversely, and time exits at the close. On the entry session, the pre-entry open is not treated as an executable exit. End-of-run holdings remain marked and incur no hypothetical liquidation charge.

The primary vectorbt path executes shifted close-derived signals at subsequent opens, uses a shared cash group for portfolio replay, and marks holdings at supplied closes (opens if no closes are supplied). Its position allocation is a fraction of remaining cash. A 60% allocation across two symbols invests $600 then $240 from $1,000; a common 10% appreciation produces $84, verified by the final review test.

DCF values require explicit filing-sourced shares and a positive finite value. Availability begins after the filing date. A newer failed or unverifiable filing invalidates the older value. A/B comparisons reuse a cached dataset, common eligible cohort, and date bounds, with exclusions disclosed. Swing windows restart flat and exclude prior setups while retaining indicator warmup; primary windows segment continuous equity.

## Verified original defects and corrections


| Finding | Impact | Correction and evidence |
| --- | --- | --- |
| Future exits settled while iterating earlier candidates | Critical chronological leakage: funds and risk capacity could be reused before the actual exit | Daily shared ledger; prefix-invariance, delayed-exit risk reservation, same-day exit-proceeds tests. |
| Remaining cash used as account equity | Incorrect stop-risk allocation across simultaneous holdings | Size against current marked equity; two flat positions each reserve $100 risk from a $10,000 account at 1%. |
| Entry-session exit evaluation skipped | Missed immediate losses and incorrect holding paths | Evaluate entry-session stops/targets; adverse daily-bar ambiguity assumes stop first. Synthetic stop loses exactly $100. |
| Open positions missing from portfolio metrics | Incorrect terminal value, returns, and drawdown | Daily closing marks, cash reconciliation, open holdings and unrealized P&L included; synthetic terminal equity $10,200 reconciles to cash plus holdings. |
| Fee/slippage accounting and affordability gaps | Potential rounding-created cash, overdraft, or understated costs | Exact unrounded costs on entry and exit; reject unaffordable entries; analytical cost/cash tests. |
| Numeric DCF value promoted to filing provenance | Invalid historical valuation eligibility | Require explicit filing-sourced shares and positive finite value; a newer failed/unverifiable filing invalidates the older value. |
| Same-day SEC filing available at session open | Date-only publication timing leakage | Values available strictly after the filing date; boundary test. |
| Closed-trade buckets presented as swing windows | Imported earlier outcomes instead of independent validation | Re-simulate each window flat, with earlier bars only for indicator warmup; exclude earlier setups and mark terminal holdings. |
| Variants fetched separate data and could use different cohorts | A/B results not comparable on equal inputs; CLI omitted windows | Cache shared OHLCV/DCF, intersect eligible cohort and date bounds, report exclusions and windows; mocked comparison test verifies fetch reuse. |
| Open winning trades included in win rate | Unrealized gains counted as completed wins | Closed-only win rate, explicit open/closed counts, undefined statistics become JSON null. |
| Open prices also used for valuation | Overnight/intraday closing equity changes omitted | Separate execution opens from closing marks; explicit mark-to-market test. |
| Misreported cost settings and inconsistent metrics | Reports could claim zero costs, use calendar-day Sharpe scaling, or inconsistent drawdown sign | Report actual costs, 252-session annualization, positive drawdown magnitude. |
| First-bar loss excluded from drawdown peak | Entry fees could produce zero reported drawdown | Include initial capital; a 1% fee on an all-cash entry yields approximately 0.990099% drawdown. |
| Invalid parameters or histories accepted | Nonsensical simulations or ambiguous chronology | Reject invalid capital, cost rates, allocations, window counts, nonfinite/nonpositive prices, duplicate/unsorted dates and misaligned signals. |
| Watchlist driver performed work at import | Importing could start external research | Explicit `main()` guard; failed per-ticker reports excluded from shared input. |

## Regression coverage and reproduction

All checks use the repository virtual environment. No external historical-performance job was run. Test values are analytical synthetic examples, not market performance claims.

| Exact command | Result |
| --- | --- |
| `venv/bin/python -m pytest -q tests --disable-warnings` | 212 passed, 0 failed, 0 skipped, 258 warnings; exit 0; 21.59 seconds. |
| `venv/bin/python -m pytest -q tests/test_phase1_audit.py --disable-warnings` | 33 passed, 0 failed, 0 skipped, 0 warnings; exit 0; 17.15 seconds. |
| `venv/bin/python -m compileall -q backtesting` | Exit 0; no compilation errors. |
| `git diff --check` | Exit 0; no whitespace errors. |
| `git diff --cached --check` | Exit 0 after exact-path staging; no whitespace errors. |

The original 33 adversarial audit cases remain unchanged. Three final-review cases add fractional-share/equity reconciliation, an analytically identifiable shared-allocation return, and gap-loss settlement ($400 realized loss against $100 planned risk). Original regressions additionally cover next-session execution, costs, aggregate risk, empty/no-overlap input, and unchanged production thresholds. Audit tests cover prefix invariance, delayed exits, same-session stops, open equity, invalid histories/economics, DCF timing/provenance, common cohorts, independent swing windows, deterministic order, and first-bar drawdown.

Warnings are counted even with `--disable-warnings`, which suppresses their detailed display, not failures. No required test command was omitted. Remote DNS failure was an inspection-environment error, not a test failure. Paid-provider availability, historical returns, and live integration tests were deliberately not performed within the authorized scope.

## Known limitations and remaining risks


1. **Two sizing models:** swing uses stop-risk sizing on equity with the existing sizer's default 0.0001-share increment with an aggregate planned-risk cap. The primary vectorbt runner allocates a fraction of remaining cash and allows fractional shares. Neither report should imply identical live execution. Planned stop risk excludes transaction costs and does not bound realized gap losses.
2. **Precomputed primary signals:** position-state filtering occurs before portfolio fills. Rejected entries, cash competition, and clipping to common dates do not regenerate that state. Shared output measures a signal replay; it can omit entries that a fully execution-coupled strategy would consider. The primary entry eligibility check also uses configured `STARTING_CAPITAL`, rather than changing portfolio equity.
3. **Different windows:** primary windows segment one continuous equity path and carry prior equity/holdings. Swing windows start flat and independently admit setups formed inside each window. Neither is walk-forward training or statistical evidence of out-of-sample predictive power.
4. **Daily-bar ambiguity:** swing entries precede all same-day exits, conservatively preventing reuse of unknown intraday proceeds. Alphabetical priority selects competing entries. An entry-day low may precede the breakout; the simulator assumes the adverse stop outcome. Daily data cannot resolve actual intraday ordering, spreads, liquidity, or partial fills.
5. **Data/universe bias:** a current static watchlist is not a historical membership dataset and can have survivorship bias. Primary shared results use the intersection of available sessions. Swing carries last available marks through missing sessions, which can understate risk from stale or delisted securities. No corporate-action or provider-data accuracy audit was performed.
6. **DCF limitations:** historical filing-date context and explicit share-count provenance reduce known input leakage; they do not prove modern LLM-generated assumptions lack future knowledge. Filing dates lack intraday acceptance timestamps, so same-date use is conservatively prohibited. Failures can exclude tickers from both compared variants, producing a selected cohort that must be disclosed.
7. **Cost assumptions:** primary fees and slippage default to zero unless configured. Swing uses configuration slippage in basis points and `BACKTEST_FEES_PCT` for fees. Callers supplying only open prices to the primary API receive open-marked equity unless closing marks are provided. Terminal holdings are valued, not forcibly liquidated; hypothetical future liquidation costs are excluded.
8. **Unsupported inputs:** historical sentiment variants C/D remain explicitly unsupported. No substitute sentiment, new strategy, model, framework, or Phase 2 feature was introduced.

These limitations must accompany any interpretation of reported returns. Passing accounting regressions is not a trading recommendation or unattended-operation sign-off.

## Phase 1.5 recommendations (not implemented)

1. Couple primary strategy state to accepted simulated fills, using current equity for entry eligibility and regenerating signals at independent window boundaries.
2. Add reproducible dataset manifests and explicit missing-session, delisting, corporate-action, and historical-universe policies.
3. Preserve contemporaneous valuation inputs and acceptance timestamps; evaluate LLM knowledge contamination separately from filing provenance.
4. Establish explicit asset-level share increments, realistic cost scenarios, and sensitivity to alphabetical allocation and daily-bar ambiguity.
5. Review the existing warnings and pin numerical/reporting conventions. Version changed report schemas for downstream consumers: pooled statistics are replaced by shared portfolio results and chronological windows.

These are recommendations only. No Phase 1.5/Phase 2 implementation, new strategy, model, trading framework, or parameter optimization is part of this commit. Merge, push, deployment, and trading require separate explicit approval.
