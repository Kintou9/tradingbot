# Phase 1 independent verification

Audit date: 2026-10-10. Scope: backtest accuracy, regression verification, and documentation only.

## Review conclusion

The original passing suite did **not** establish correct shared-capital accounting. Adversarial tests reproduced future-exit cash/risk reuse, incorrect equity sizing, skipped entry-session stops, and omitted open-position equity. Those defects are corrected in the working tree, along with the reporting and provenance issues below.

The corrected implementation is ready for review as a **bounded historical simulation improvement**, subject to the assumptions below. This is not approval for live deployment or evidence of profitable trading. In particular, the primary vectorbt runner remains a precomputed signal replay; only the swing harness provides execution-coupled stop-risk sizing and independent flat-start windows. If full execution-coupled primary-strategy validation is a merge requirement, that requirement remains unmet and this audit does not certify it.

## Branch, scope, and preservation

- Active branch: `backtest-accuracy-phase1`.
- Local `HEAD` and local `main`: `a83bececd80bfbaeab185222a6897f1f7acf1492`.
- There were no committed branch differences: the submitted Phase 1 implementation was entirely uncommitted. Both the working-tree diff against `main` and untracked implementation files were inspected. Remote `main` was not fetched or independently verified.
- Initially modified tracked files: `backtesting/backtest_runner.py`, `backtesting/swing_backtest.py`, `backtesting/run_watchlist_backtest.py`, `backtesting/run_swing_backtest.py`.
- Initially added implementation files: `backtesting/metrics.py`, `tests/test_backtest_regression_guards.py`, `tests/test_backtest_runner_execution.py`, `tests/test_swing_backtest_sizing.py`.
- No deleted files. Additional pre-existing untracked `PROJECT_HISTORY.md` was unrelated user work and remains byte-identical. The original three Phase 1 test files also remain byte-identical.
- This audit modifies the four backtesting files and the untracked metrics helper, and adds `tests/test_phase1_audit.py` and this document. No production engine, API, execution, strategy, dependency, or environment file was edited.
- Initial files, SHA-256 manifest, binary patch, and main archive were preserved at `/var/folders/tb/8p2k_3f53lz55csh02kn0p2w0000gn/T/tradingbot-phase1-before-zkx5cxdd`. This is a local temporary backup, not a committed artifact.
- No commit, merge, push, deployment, broker action, external-data backtest, or paid research run was performed.

## Previous claims checked

| Previous claim | Independently observed |
| --- | --- |
| 176 tests passed, zero failed | Confirmed before changes: 176 passed, 258 warnings, 10.81 seconds. |
| 24 new tests | Confirmed: isolated local main had 152 passed, 258 warnings, 3.83 seconds; submitted tree had 176. Counts include parametrized cases. |
| Four existing files modified, four new files | Confirmed for Phase 1 scope; unrelated untracked `PROJECT_HISTORY.md` additionally existed. |
| No live/production decision changes | Confirmed by changed-file scope. Historical backtest adapters import existing production evaluators but do not modify them. |
| Missing `share_count_source: "filing"` corrected | Presence alone was insufficient: the submitted adapter could label an unverified numeric DCF result as filing-sourced. The loader now requires explicit provenance. |

The first independent adversarial batch produced **9 failures and 2 passes** against the submitted implementation. This demonstrates that a green original suite did not prove the claimed accounting properties.

## Findings and corrections

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

## Validation and reproduction

Use the repository virtual environment:

```sh
venv/bin/python -m pytest -q tests --disable-warnings
venv/bin/python -m pytest -q tests/test_phase1_audit.py --disable-warnings
git diff --check
venv/bin/python -m compileall -q backtesting
```

Final suite: **209 passed, zero failed, 258 warnings**. This is the 176-test submitted baseline plus **33 independent audit cases**. Existing warning count is unchanged; warnings were not fixed as part of this scope. Tests use synthetic price histories and mocked external inputs; they do not validate provider availability or actual market performance.

The tests cover future-data prefix invariance, cash/risk reservation, same-session stop behavior, exact cost reconciliation, open-position valuation, held-position daily drawdown, listing-date boundaries, independent swing windows, DCF provenance and timing, common-cohort comparison, unsupported sentiment variants, ticker-order determinism, first-bar loss, and input validation. Existing execution-shift and regression-guard tests also pass.

Main was tested from an isolated `git archive main` extraction using the same virtual environment. Audit logs are local temporary files `/private/tmp/phase1-audit-reproductions.txt` and `/private/tmp/phase1-final-tests.txt`; durable evidence is the committed-candidate tests and this report.

## Model boundaries and remaining limitations

1. **Two sizing models:** swing uses whole-share stop-risk sizing on equity with an aggregate planned-risk cap. The primary vectorbt runner allocates a fraction of remaining cash and allows fractional shares. Neither report should imply identical live execution. Planned stop risk excludes transaction costs and does not bound realized gap losses.
2. **Precomputed primary signals:** position-state filtering occurs before portfolio fills. Rejected entries, cash competition, and clipping to common dates do not regenerate that state. Shared output measures a signal replay; it can omit entries that a fully execution-coupled strategy would consider. The primary entry eligibility check also uses configured `STARTING_CAPITAL`, rather than changing portfolio equity.
3. **Different windows:** primary windows segment one continuous equity path and carry prior equity/holdings. Swing windows start flat and independently admit setups formed inside each window. Neither is walk-forward training or statistical evidence of out-of-sample predictive power.
4. **Daily-bar ambiguity:** swing entries precede all same-day exits, conservatively preventing reuse of unknown intraday proceeds. Alphabetical priority selects competing entries. An entry-day low may precede the breakout; the simulator assumes the adverse stop outcome. Daily data cannot resolve actual intraday ordering, spreads, liquidity, or partial fills.
5. **Data/universe bias:** a current static watchlist is not a historical membership dataset and can have survivorship bias. Primary shared results use the intersection of available sessions. Swing carries last available marks through missing sessions, which can understate risk from stale or delisted securities. No corporate-action or provider-data accuracy audit was performed.
6. **DCF limitations:** historical filing-date context and explicit share-count provenance reduce known input leakage; they do not prove modern LLM-generated assumptions lack future knowledge. Filing dates lack intraday acceptance timestamps, so same-date use is conservatively prohibited. Failures can exclude tickers from both compared variants, producing a selected cohort that must be disclosed.
7. **Cost assumptions:** primary fees and slippage default to zero unless configured. Swing uses configuration slippage in basis points and `BACKTEST_FEES_PCT` for fees. Callers supplying only open prices to the primary API receive open-marked equity unless closing marks are provided. Terminal holdings are valued, not forcibly liquidated; hypothetical future liquidation costs are excluded.
8. **Unsupported inputs:** historical sentiment variants C/D remain explicitly unsupported. No substitute sentiment, new strategy, model, framework, or Phase 2 feature was introduced.

These limitations must accompany any interpretation of reported returns. Passing accounting regressions is not a trading recommendation or unattended-operation sign-off.
