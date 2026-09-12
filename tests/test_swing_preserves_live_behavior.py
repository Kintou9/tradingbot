"""
Confirms the experimental swing-strategy package (engine.swing_strategy)
is additive only: the live entry/exit logic and its constants behave
exactly as they did before this package existed, and importing the new
package has no side effect on the live tables.
"""

import engine.swing_strategy.config  # noqa: F401 — importing must not error or side-effect anything
import engine.swing_strategy.engine  # noqa: F401
from engine.entry_exit import signals


def test_live_valuation_threshold_unchanged():
    assert signals.VALUATION_DISCOUNT_THRESHOLD == 0.20


def test_live_sentiment_threshold_unchanged():
    assert signals.MIN_SENTIMENT_SCORE == 0.0


def test_live_evaluate_entry_still_rejects_non_filing_share_count():
    # This is the fail-closed fix from 2026-09-09/11 — confirms the swing
    # package didn't get bundled in a way that altered it.
    valuation = {"estimated_fair_value_per_share": 1000, "share_count_source": "unavailable"}
    technical = {"entry_price": 100, "trend": "up", "probability_estimate": 80}
    result = signals.evaluate_entry("TEST", valuation, technical, 0.5, max_position_pct=0.1, account_equity=1000)
    assert result is None


def test_live_evaluate_entry_still_accepts_a_clean_signal():
    valuation = {"estimated_fair_value_per_share": 130, "share_count_source": "filing"}
    technical = {"entry_price": 100, "trend": "up", "probability_estimate": 80}
    result = signals.evaluate_entry("TEST", valuation, technical, 0.5, max_position_pct=0.1, account_equity=1000)
    assert result is not None
    assert result.action == signals.SignalAction.BUY


def test_live_evaluate_exit_unchanged_stop_and_target_behavior():
    stop_signal = signals.evaluate_exit("TEST", entry_price=100, current_price=94, stop_loss_price=95, take_profit_price=110)
    assert stop_signal is not None
    assert stop_signal.reason == "stop_loss"

    target_signal = signals.evaluate_exit("TEST", entry_price=100, current_price=111, stop_loss_price=95, take_profit_price=110)
    assert target_signal is not None
    assert target_signal.reason == "take_profit"

    no_signal = signals.evaluate_exit("TEST", entry_price=100, current_price=102, stop_loss_price=95, take_profit_price=110)
    assert no_signal is None


def test_swing_models_are_separate_tables_from_live_ones():
    from db.models import Position, Trade, SwingPosition, SwingTrade, SwingCandidateLog
    live_tables = {Position.__tablename__, Trade.__tablename__}
    swing_tables = {SwingPosition.__tablename__, SwingTrade.__tablename__, SwingCandidateLog.__tablename__}
    assert live_tables.isdisjoint(swing_tables)


def test_swing_strategy_never_reaches_the_live_trading_cycle():
    # The real safety boundary: functions/market_hours_trading.py is the
    # function both the manual-trigger and the LIVE autonomous-mode loop
    # in api/main.py call to trade the real (paper-account) MSFT/AVPT-style
    # positions. That file must never reference the experimental swing
    # package, directly or through functions.swing_paper_trading.
    #
    # api/main.py is EXEMPT from this check as of 2026-09-12: it now also
    # runs a separate, independently-toggled swing-autonomous background
    # loop (functions.swing_paper_trading + db.swing_autonomous_mode) —
    # by explicit user request, to accumulate real paper-trading history
    # for the swing strategy. That is paper execution into the isolated
    # SwingPosition/SwingTrade tables, never the live Position/Trade
    # tables, and never through run_market_hours_trading — see
    # test_swing_paper_trading_isolated_from_live_tables below for that
    # guarantee instead.
    import pathlib
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    source = (repo_root / "functions/market_hours_trading.py").read_text()
    assert "swing_strategy" not in source
    assert "swing_paper_trading" not in source


def test_swing_paper_trading_isolated_from_live_tables():
    # functions/swing_paper_trading.py (the module the swing-autonomous
    # loop actually calls) must never import the live Position/Trade
    # tables — only their Swing-prefixed counterparts. Checked via ast,
    # not substring matching, since "SwingPosition" itself contains the
    # substring "Position".
    import ast
    import pathlib
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    source = (repo_root / "functions/swing_paper_trading.py").read_text()
    tree = ast.parse(source)
    imported_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "db.models"
        for alias in node.names
    }
    assert "Position" not in imported_names
    assert "Trade" not in imported_names
    assert "SwingPosition" in imported_names
    assert "SwingTrade" in imported_names
