from engine.swing_strategy.exits import evaluate_swing_exit_bar, evaluate_swing_exit_live, SwingExitReason


def test_stop_loss_wins_precedence_over_time_based():
    result = evaluate_swing_exit_live(
        current_price=94, stop_price=95, target_price=110,
        sessions_held=25, max_holding_trading_days=20,
    )
    assert result.should_exit
    assert result.reason == SwingExitReason.STOP_LOSS


def test_take_profit_fires_before_time_based_if_stop_not_hit():
    result = evaluate_swing_exit_live(
        current_price=111, stop_price=95, target_price=110,
        sessions_held=25, max_holding_trading_days=20,
    )
    assert result.reason == SwingExitReason.TAKE_PROFIT


def test_time_based_exit_fires_only_when_neither_price_level_hit():
    result = evaluate_swing_exit_live(
        current_price=101, stop_price=95, target_price=110,
        sessions_held=20, max_holding_trading_days=20,
    )
    assert result.should_exit
    assert result.reason == SwingExitReason.TIME_BASED_EXIT


def test_no_exit_when_nothing_triggered():
    result = evaluate_swing_exit_live(
        current_price=101, stop_price=95, target_price=110,
        sessions_held=5, max_holding_trading_days=20,
    )
    assert not result.should_exit
    assert result.reason is None


def test_duplicate_exit_order_suppressed():
    result = evaluate_swing_exit_live(
        current_price=90, stop_price=95, target_price=110,
        sessions_held=5, max_holding_trading_days=20,
        duplicate_exit_pending=True,
    )
    assert not result.should_exit
    assert "duplicate" in result.note


def test_bar_gap_down_through_stop_fills_at_open_not_stop_price():
    result = evaluate_swing_exit_bar(
        open_price=90, high=91, low=88, close=89,
        stop_price=95, target_price=110,
        sessions_held=1, max_holding_trading_days=20,
    )
    assert result.should_exit
    assert result.reason == SwingExitReason.STOP_LOSS
    assert result.exit_price == 90  # the gap open, not the stale stop level


def test_bar_gap_up_through_target_fills_at_open():
    result = evaluate_swing_exit_bar(
        open_price=115, high=116, low=114, close=115,
        stop_price=95, target_price=110,
        sessions_held=1, max_holding_trading_days=20,
    )
    assert result.reason == SwingExitReason.TAKE_PROFIT
    assert result.exit_price == 115


def test_bar_touches_both_stop_and_target_assumes_stop():
    # A wide-range bar that opens between the two levels but whose
    # low/high both breach stop and target — order can't be known.
    result = evaluate_swing_exit_bar(
        open_price=100, high=112, low=93, close=105,
        stop_price=95, target_price=110,
        sessions_held=1, max_holding_trading_days=20,
    )
    assert result.reason == SwingExitReason.STOP_LOSS
    assert result.exit_price == 95
    assert "can't be determined" in result.note


def test_bar_touches_only_stop():
    result = evaluate_swing_exit_bar(
        open_price=100, high=101, low=93, close=97,
        stop_price=95, target_price=110,
        sessions_held=1, max_holding_trading_days=20,
    )
    assert result.reason == SwingExitReason.STOP_LOSS
    assert result.exit_price == 95


def test_bar_touches_only_target():
    result = evaluate_swing_exit_bar(
        open_price=100, high=111, low=99, close=108,
        stop_price=95, target_price=110,
        sessions_held=1, max_holding_trading_days=20,
    )
    assert result.reason == SwingExitReason.TAKE_PROFIT
    assert result.exit_price == 110


def test_bar_time_based_exit_at_close_when_neither_level_hit():
    result = evaluate_swing_exit_bar(
        open_price=100, high=102, low=99, close=101,
        stop_price=95, target_price=110,
        sessions_held=20, max_holding_trading_days=20,
    )
    assert result.reason == SwingExitReason.TIME_BASED_EXIT
    assert result.exit_price == 101  # executed at the bar's close


def test_bar_duplicate_exit_suppressed():
    result = evaluate_swing_exit_bar(
        open_price=90, high=91, low=88, close=89,
        stop_price=95, target_price=110,
        sessions_held=1, max_holding_trading_days=20,
        duplicate_exit_pending=True,
    )
    assert not result.should_exit
