"""Offline primary backtest contracts. No execution, services, or environment reads."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum, IntEnum
import json
import math
from numbers import Real
import re
from types import MappingProxyType
from typing import Mapping

import pandas as pd

from engine.entry_exit.signals import MIN_TRADE_NOTIONAL


def number(value, name, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if value < 0 or (positive and value == 0):
        raise ValueError(f"{name} must be {'positive' if positive else 'nonnegative'}")


def symbol_name(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.\-]*", value.strip()):
        raise ValueError("Invalid symbol")
    return value.strip().upper()


def session_date(value):
    if type(value) is not date:
        raise ValueError("Session must be a datetime.date")


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(str, Enum):
    PENDING = "pending"
    FILLED = "filled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class RejectionReason(str, Enum):
    INSUFFICIENT_CASH = "insufficient_cash"
    BELOW_MIN_NOTIONAL = "below_min_notional"
    QUANTITY_ROUNDS_TO_ZERO = "quantity_rounds_to_zero"
    STALE_PORTFOLIO_MARKS = "stale_portfolio_marks"
    INVALID_EXECUTION_EQUITY = "invalid_execution_equity"


class ExpirationReason(str, Enum):
    MISSING_EXECUTION_BAR = "missing_execution_bar"
    END_OF_WINDOW = "end_of_window"
    END_OF_DATASET = "end_of_dataset"


class ValuationMode(str, Enum):
    VERIFIED = "verified_filing_inputs"
    LEGACY = "legacy_research_inputs"


class ProvenanceStatus(str, Enum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"


class Phase(IntEnum):
    OPEN = 0
    CLOSE = 1


class MarkQuality(str, Enum):
    CURRENT = "current"
    STALE = "stale"
    MISSING = "missing"


@dataclass(frozen=True, order=True)
class EventTime:
    session: date
    phase: Phase
    sequence: int = 0

    def __post_init__(self):
        session_date(self.session)
        if not isinstance(self.phase, Phase) or type(self.sequence) is not int or self.sequence < 0:
            raise ValueError("Invalid event phase/sequence")


@dataclass(frozen=True)
class Mark:
    """Source phase is distinct from snapshot time; missing has no invented value."""
    value: float | None
    observed_at: EventTime | None
    as_of: EventTime
    quality: MarkQuality

    def __post_init__(self):
        if not isinstance(self.quality, MarkQuality):
            raise ValueError("Invalid mark quality")
        if self.quality == MarkQuality.MISSING:
            if self.value is not None or self.observed_at is not None:
                raise ValueError("Missing mark cannot have a price/source")
            return
        number(self.value, "mark", positive=True)
        if self.observed_at is None or self.observed_at > self.as_of:
            raise ValueError("Mark cannot come from the future")
        same_phase = (self.observed_at.session, self.observed_at.phase) == (self.as_of.session, self.as_of.phase)
        if (self.quality == MarkQuality.CURRENT) != same_phase:
            raise ValueError("Mark quality disagrees with source phase")


@dataclass(frozen=True)
class PrimarySimulationConfig:
    initial_cash: float
    allocation_pct: float
    fee_pct: float = 0.0
    slippage_pct: float = 0.0
    quantity_increment: float | None = None
    min_trade_notional: float = MIN_TRADE_NOTIONAL
    valuation_mode: ValuationMode = ValuationMode.VERIFIED
    calendar_policy: str = "observed_union"
    ordering_policy: str = "sells_then_buys_alphabetical_v1"
    schema_version: str = "primary_execution_coupled_v1"
    strategy_version: str = "primary_historical_v1"
    annualization_sessions: int = 252

    def __post_init__(self):
        number(self.initial_cash, "initial_cash", positive=True)
        number(self.allocation_pct, "allocation_pct", positive=True)
        if self.allocation_pct > 1:
            raise ValueError("allocation_pct must be <= 1")
        for name in ("fee_pct", "slippage_pct"):
            number(getattr(self, name), name)
            if getattr(self, name) >= 1:
                raise ValueError(f"{name} must be < 1")
        if self.quantity_increment is not None:
            number(self.quantity_increment, "quantity_increment", positive=True)
        number(self.min_trade_notional, "min_trade_notional", positive=True)
        if self.min_trade_notional != MIN_TRADE_NOTIONAL:
            raise ValueError("Historical minimum notional is not a tunable threshold")
        object.__setattr__(self, "valuation_mode", ValuationMode(self.valuation_mode))
        if self.calendar_policy not in {"observed_union", "archived_exchange_calendar"}:
            raise ValueError("Unknown calendar policy")
        if (self.ordering_policy != "sells_then_buys_alphabetical_v1" or
                self.schema_version != "primary_execution_coupled_v1" or
                self.strategy_version != "primary_historical_v1" or self.annualization_sessions != 252):
            raise ValueError("Unsupported policy/version")

    @property
    def applied_controls(self):
        return ("remaining_cash_allocation", "minimum_notional", "no_leverage", "one_position_per_symbol")

    @property
    def unmodeled_live_controls(self):
        return ("max_trade_dollars", "max_entries_per_day", "live_exposure_cap", "daily_loss_pause")


@dataclass(frozen=True)
class FactEvidence:
    fact_id: str
    available_date: date
    source_id: str
    context_hash: str

    def __post_init__(self):
        session_date(self.available_date)
        if not self.fact_id or not self.source_id or not re.fullmatch(r"[0-9a-f]{64}", self.context_hash):
            raise ValueError("Fact evidence requires identity, source and SHA-256 context hash")


@dataclass(frozen=True)
class ValuationObservation:
    symbol: str
    filed_date: date
    effective_available_date: date
    fair_value: float | None
    share_count_source: str
    filing_context_hash: str | None = None
    raw_output_hash: str | None = None
    model_version: str | None = None
    prompt_version: str | None = None
    generated_at: datetime | None = None
    accession_id: str | None = None
    required_fact_ids: tuple[str, ...] = ()
    fact_evidence: tuple[FactEvidence, ...] = ()
    invalidation_reason: str | None = None
    # Reconstructed LLM outputs are never certified knowledge-pure here.
    knowledge_contamination_status: str = "unverified"

    def __post_init__(self):
        object.__setattr__(self, "symbol", symbol_name(self.symbol))
        session_date(self.filed_date)
        session_date(self.effective_available_date)
        if self.effective_available_date <= self.filed_date:
            raise ValueError("Valuation availability must be strictly after filing date")
        if self.fair_value is None:
            if not self.invalidation_reason:
                raise ValueError("Null fair value requires explicit invalidation")
        else:
            number(self.fair_value, "fair_value", positive=True)
            if self.invalidation_reason:
                raise ValueError("Invalidation must not also contain a fair value")
        for name in ("filing_context_hash", "raw_output_hash"):
            value = getattr(self, name)
            if value is not None and not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError(f"{name} must be SHA-256")
        if self.generated_at is not None and (not isinstance(self.generated_at, datetime) or
                self.generated_at.tzinfo is None or self.generated_at.utcoffset() is None):
            raise ValueError("Generation timestamp must be timezone-aware")
        if self.knowledge_contamination_status != "unverified":
            raise ValueError("Historical LLM knowledge purity is not certified")
        ids = tuple(self.required_fact_ids)
        evidence = tuple(self.fact_evidence)
        if len(set(ids)) != len(ids) or any(not isinstance(i, str) or not i for i in ids):
            raise ValueError("Invalid/duplicate required fact IDs")
        if any(not isinstance(e, FactEvidence) for e in evidence) or len({e.fact_id for e in evidence}) != len(evidence):
            raise ValueError("Invalid/duplicate fact evidence")
        object.__setattr__(self, "required_fact_ids", tuple(sorted(ids)))
        object.__setattr__(self, "fact_evidence", tuple(sorted(evidence, key=lambda e: e.fact_id)))


@dataclass(frozen=True)
class PreparedValuation:
    observation: ValuationObservation
    eligible: bool
    provenance: ProvenanceStatus
    reason: str | None


@dataclass(frozen=True)
class PreparedFrame:
    symbol: str
    dates: tuple[date, ...]
    columns: tuple[str, ...]
    rows: tuple[tuple[float | None, ...], ...]

    def to_frame(self):
        return pd.DataFrame(self.rows, index=pd.DatetimeIndex(self.dates), columns=self.columns, dtype=float)


@dataclass(frozen=True)
class PreparedPrimaryData:
    symbols: tuple[str, ...]
    sessions: tuple[date, ...]
    frames: tuple[PreparedFrame, ...]
    valuations: tuple[PreparedValuation, ...]
    valuation_mode: ValuationMode
    metadata_json: str
    quality_json: str
    content_hash: str

    @property
    def prices_by_symbol(self):
        return {frame.symbol: frame.to_frame() for frame in self.frames}

    @property
    def session_index(self):
        return pd.DatetimeIndex(self.sessions)

    @property
    def metadata(self):
        return json.loads(self.metadata_json)

    @property
    def data_quality(self):
        return json.loads(self.quality_json)

    def valuation_asof(self, symbol: str, session: date) -> PreparedValuation | None:
        """Latest available observation, including ineligible invalidations."""
        session_date(session)
        matches = [v for v in self.valuations if v.observation.symbol == symbol_name(symbol)
                   and v.observation.effective_available_date <= session]
        return matches[-1] if matches else None


@dataclass(frozen=True)
class Position:
    symbol: str
    quantity: float
    entry_order_id: str
    signal_at: EventTime
    entry_at: EventTime
    reference_entry_price: float
    entry_price: float
    entry_fee: float
    cost_basis: float
    stop_price: float
    target_price: float
    mark: Mark

    def __post_init__(self):
        if self.symbol != symbol_name(self.symbol) or not self.entry_order_id:
            raise ValueError("Position requires normalized symbol and entry ID")
        for name in ("quantity", "reference_entry_price", "entry_price", "cost_basis", "stop_price", "target_price"):
            number(getattr(self, name), name, positive=True)
        number(self.entry_fee, "entry_fee")
        if self.signal_at.phase != Phase.CLOSE or self.entry_at.phase != Phase.OPEN or self.entry_at.session <= self.signal_at.session:
            raise ValueError("Entry must follow signal at a later open")
        if not math.isclose(self.cost_basis, self.quantity*self.entry_price+self.entry_fee, rel_tol=1e-10, abs_tol=1e-8):
            raise ValueError("Entry cost basis does not reconcile")
        if self.mark.value is None or self.mark.as_of < self.entry_at or self.mark.observed_at < self.entry_at:
            raise ValueError("Held position requires a post-entry mark")


@dataclass(frozen=True)
class OrderIntent:
    order_id: str
    symbol: str
    side: Side
    signal_at: EventTime
    eligible_session: date | None
    reason: str
    confidence: float
    decision_equity: float
    allocation_pct: float | None = None
    position_id: str | None = None
    stop_price: float | None = None
    target_price: float | None = None
    status: OrderStatus = OrderStatus.PENDING
    input_id: str = ""

    def __post_init__(self):
        if not self.order_id or self.symbol != symbol_name(self.symbol) or not isinstance(self.side, Side):
            raise ValueError("Invalid intent identity/side")
        if self.status != OrderStatus.PENDING or self.signal_at.phase != Phase.CLOSE:
            raise ValueError("Intent must be pending and originate at close")
        if self.eligible_session is not None:
            session_date(self.eligible_session)
            if self.eligible_session <= self.signal_at.session:
                raise ValueError("Intent execution must be a later session")
        number(self.confidence, "confidence")
        number(self.decision_equity, "decision_equity", positive=True)
        if self.confidence > 1 or not self.reason:
            raise ValueError("Invalid intent confidence/reason")
        if self.side == Side.BUY:
            number(self.allocation_pct, "allocation_pct", positive=True)
            number(self.stop_price, "stop_price", positive=True)
            number(self.target_price, "target_price", positive=True)
            if self.allocation_pct > 1 or self.position_id is not None:
                raise ValueError("Invalid buy intent")
        elif not self.position_id or any(v is not None for v in (self.allocation_pct, self.stop_price, self.target_price)):
            raise ValueError("Sell must reference a holding, not entry sizing")


@dataclass(frozen=True)
class Fill:
    order_id: str
    symbol: str
    side: Side
    signal_at: EventTime
    at: EventTime
    quantity: float
    reference_price: float
    price: float
    fee: float
    slippage_cost: float
    cash_delta: float

    def __post_init__(self):
        if not self.order_id or self.symbol != symbol_name(self.symbol) or not isinstance(self.side, Side):
            raise ValueError("Invalid fill identity")
        if self.signal_at.phase != Phase.CLOSE or self.at.phase != Phase.OPEN or self.at.session <= self.signal_at.session:
            raise ValueError("Fill must occur at a later open")
        for n in ("quantity", "reference_price", "price"):
            number(getattr(self, n), n, positive=True)
        number(self.fee, "fee")
        number(self.slippage_cost, "slippage_cost")
        if self.fee >= self.quantity*self.price:
            raise ValueError("Fill fee must be less than notional")
        expected = -(self.quantity*self.price+self.fee) if self.side == Side.BUY else self.quantity*self.price-self.fee
        if not math.isfinite(self.cash_delta) or not math.isclose(self.cash_delta, expected, abs_tol=1e-8):
            raise ValueError("Fill cash delta does not reconcile")
        slip = self.quantity * (self.price-self.reference_price) * (1 if self.side == Side.BUY else -1)
        if not math.isclose(slip, self.slippage_cost, abs_tol=1e-8):
            raise ValueError("Fill slippage does not reconcile")


@dataclass(frozen=True)
class OrderOutcome:
    order_id: str
    status: OrderStatus
    at: EventTime
    reason: RejectionReason | ExpirationReason | None = None
    fill: Fill | None = None

    def __post_init__(self):
        if not self.order_id or not isinstance(self.status, OrderStatus) or self.status == OrderStatus.PENDING:
            raise ValueError("Outcome must be terminal")
        if self.status == OrderStatus.FILLED:
            if self.fill is None or self.fill.order_id != self.order_id or self.fill.at != self.at or self.reason is not None:
                raise ValueError("Filled outcome requires matching fill")
        elif self.fill is not None or not isinstance(self.reason, RejectionReason if self.status == OrderStatus.REJECTED else ExpirationReason):
            raise ValueError("Nonfill requires reason and cannot contain fill")

    @property
    def cash_delta(self):
        return self.fill.cash_delta if self.fill else 0.0


@dataclass(frozen=True)
class PortfolioView:
    at: EventTime
    cash: float
    equity: float
    exposure: float
    positions: Mapping[str, Position]
    pending: Mapping[str, OrderIntent]

    def __post_init__(self):
        for key, position in self.positions.items():
            if key != position.symbol or position.mark.as_of != self.at:
                raise ValueError("Decision view contains mismatched position/time")
        for key, intent in self.pending.items():
            if key != intent.symbol or intent.signal_at > self.at:
                raise ValueError("Decision view contains mismatched intent/time")
        for n in ("cash", "equity", "exposure"):
            number(getattr(self, n), n)
        object.__setattr__(self, "positions", MappingProxyType(dict(self.positions)))
        object.__setattr__(self, "pending", MappingProxyType(dict(self.pending)))


class DecisionAction(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class DecisionRecord:
    at: EventTime
    symbol: str
    action: DecisionAction
    reason: str
    observed_quantity: float
    input_id: str
    pending_id: str | None = None
    intent: OrderIntent | None = None

    def __post_init__(self):
        number(self.observed_quantity, "observed_quantity")
        if self.at.phase != Phase.CLOSE or self.symbol != symbol_name(self.symbol) or not isinstance(self.action, DecisionAction):
            raise ValueError("Invalid closing decision")
        if (self.action in (DecisionAction.BUY, DecisionAction.SELL)) != (self.intent is not None):
            raise ValueError("Trade decision must have intent")
        if self.intent and (self.intent.symbol != self.symbol or self.intent.signal_at != self.at or self.intent.side.value != self.action.value):
            raise ValueError("Decision intent mismatch")


@dataclass
class PortfolioState:
    at: EventTime
    initial_cash: float
    cash: float
    equity: float
    positions: dict[str, Position] = field(default_factory=dict)
    pending: dict[str, OrderIntent] = field(default_factory=dict)
    outcomes: tuple[OrderOutcome, ...] = ()
    realized_net_pnl: float = 0.0
    realized_gross_pnl: float = 0.0
    fees_paid: float = 0.0
    slippage_costs: float = 0.0
    next_sequence: int = 0

    @property
    def available_cash(self):
        return self.cash

    @property
    def reserved_cash(self):
        return 0.0

    @property
    def exposure(self):
        return sum(p.quantity*p.mark.value for p in self.positions.values())

    def validate(self):
        """Pure snapshot checks only; never applies fills or changes state."""
        number(self.initial_cash, "initial_cash", positive=True)
        if type(self.next_sequence) is not int or self.next_sequence < 0:
            raise ValueError("Invalid next sequence")
        for n in ("cash", "equity", "fees_paid", "slippage_costs"):
            number(getattr(self, n), n)
        if not all(math.isfinite(v) for v in (self.realized_net_pnl, self.realized_gross_pnl)):
            raise ValueError("Invalid realized P&L")
        if len({p.entry_order_id for p in self.positions.values()}) != len(self.positions):
            raise ValueError("Duplicate position entry ID")
        for symbol, position in self.positions.items():
            if symbol != position.symbol or position.mark.as_of != self.at:
                raise ValueError("Position key or snapshot time mismatch")
        ids = [o.order_id for o in self.outcomes] + [o.order_id for o in self.pending.values()]
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate terminal outcome or pending order ID")
        for symbol, intent in self.pending.items():
            if symbol != intent.symbol or intent.signal_at > self.at:
                raise ValueError("Pending symbol/time mismatch")
            held = self.positions.get(symbol)
            if (intent.side == Side.BUY and held is not None) or (intent.side == Side.SELL and (held is None or intent.position_id != held.entry_order_id)):
                raise ValueError("Orphan or duplicate pending intent")
        if any(o.at > self.at for o in self.outcomes):
            raise ValueError("Future outcome")
        tol = 1e-8*max(1, self.initial_cash)
        unrealized = sum(p.quantity*p.mark.value-p.cost_basis for p in self.positions.values())
        if not math.isclose(self.equity, self.cash+self.exposure, rel_tol=0, abs_tol=tol):
            raise ValueError("Equity does not reconcile to cash and holdings")
        if not math.isclose(self.equity-self.initial_cash, self.realized_net_pnl+unrealized, rel_tol=0, abs_tol=tol):
            raise ValueError("Equity does not reconcile to P&L")

    def decision_view(self):
        self.validate()
        return PortfolioView(self.at, self.cash, self.equity, self.exposure, self.positions, self.pending)


@dataclass(frozen=True)
class PortfolioSnapshot:
    at: EventTime
    cash: float
    equity: float
    exposure: float
    marks: tuple[tuple[str, Mark], ...]

    def __post_init__(self):
        for name in ("cash", "equity", "exposure"):
            number(getattr(self, name), name)
        if not math.isclose(self.equity, self.cash+self.exposure, abs_tol=1e-8):
            raise ValueError("Snapshot equity mismatch")
        marks = tuple(self.marks)
        if len({s for s, _ in marks}) != len(marks):
            raise ValueError("Duplicate snapshot mark")
        if any(s != symbol_name(s) or m.as_of != self.at for s, m in marks):
            raise ValueError("Snapshot mark time/symbol mismatch")
        object.__setattr__(self, "marks", tuple(sorted(marks)))


@dataclass(frozen=True)
class ClosedTrade:
    symbol: str
    entry: Fill
    exit: Fill

    def __post_init__(self):
        if (self.symbol != self.entry.symbol or self.symbol != self.exit.symbol or
                self.entry.side != Side.BUY or self.exit.side != Side.SELL or
                self.entry.quantity != self.exit.quantity or self.exit.at <= self.entry.at):
            raise ValueError("Closed trade must match full ordered entry/exit fills")


@dataclass(frozen=True)
class SimulationResult:
    """Future engine output container; no matching or reporting implementation."""
    config: PrimarySimulationConfig
    dataset_hash: str
    fills: tuple[Fill, ...] = ()
    outcomes: tuple[OrderOutcome, ...] = ()
    decisions: tuple[DecisionRecord, ...] = ()
    terminal_positions: tuple[Position, ...] = ()
    history: tuple[PortfolioSnapshot, ...] = ()
    closed_trades: tuple[ClosedTrade, ...] = ()
    manifest_json: str = "{}"

    def __post_init__(self):
        if not re.fullmatch(r"[0-9a-f]{64}", self.dataset_hash):
            raise ValueError("Dataset hash must be SHA-256")
        for name in ("fills", "outcomes", "decisions", "terminal_positions", "history", "closed_trades"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        ids = [o.order_id for o in self.outcomes]
        fill_ids = [f.order_id for f in self.fills]
        if len(set(ids)) != len(ids) or len(set(fill_ids)) != len(fill_ids):
            raise ValueError("Duplicate result fill/outcome")
        if len({p.symbol for p in self.terminal_positions}) != len(self.terminal_positions):
            raise ValueError("Duplicate terminal position")
        times = [h.at for h in self.history]
        if times != sorted(set(times)):
            raise ValueError("Result history must be strictly chronological")
        if not isinstance(json.loads(self.manifest_json), dict):
            raise ValueError("Result manifest must be a JSON object")
