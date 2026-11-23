"""Pinned durable envelope for the unchanged Phase 2C1 simulator."""

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from finintel.models.backtest import BACKTEST_REVISION, BacktestContractError, BacktestRequest
from finintel.models.registry import IMPLEMENTATION_REVISION, MAX_RESULT_BYTES

BACKTEST_JOB_CONTRACT = "backtest-job-request-v1"
BACKTEST_RESULT_CONTRACT = "backtest-job-result-v1"
Hash = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
JobId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")]
DecimalText = Annotated[
    str, StringConstraints(pattern=r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$", max_length=512)
]

_ASSUMPTION_DECIMALS = {
    "initial_capital",
    "position_fraction",
    "commission_bps",
    "slippage_bps",
    "risk_free_rate_annual",
}


def _fixed_point(value: object) -> bool:
    return isinstance(value, str) and bool(
        re.fullmatch(r"-?(?:0|[1-9][0-9]{0,12})(?:\.[0-9]{1,6})?", value)
    )


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class DurableBacktestRequest(Strict):
    contract_version: Literal["backtest-job-request-v1"]
    source_job_id: JobId
    source_request_sha256: Hash
    source_output_sha256: Hash
    source_lineage_sha256: Hash
    source_implementation_revision: Literal[IMPLEMENTATION_REVISION]
    engine_revision: Literal[BACKTEST_REVISION]
    simulation: BacktestRequest

    @classmethod
    def from_wire(cls, payload: bytearray) -> "DurableBacktestRequest":
        value = json.loads(payload)
        if isinstance(value, dict):
            simulation = value.get("simulation")
            settings = simulation.get("assumptions") if isinstance(simulation, dict) else None
            if isinstance(settings, dict):
                for name in _ASSUMPTION_DECIMALS & settings.keys():
                    if not _fixed_point(settings[name]):
                        raise ValueError("Durable assumptions require fixed-point decimal strings")
        return cls.model_validate_json(payload)

    @model_validator(mode="after")
    def bound_assumption_precision(self) -> "DurableBacktestRequest":
        settings = self.simulation.assumptions.model_dump(mode="json")
        if not all(_fixed_point(settings[name]) for name in _ASSUMPTION_DECIMALS):
            raise ValueError("Durable assumptions require bounded fixed-point decimals")
        return self


class EquityRow(Strict):
    session_date: str
    equity_after_cost: DecimalText
    frictionless_benchmark_equity: DecimalText
    exposure_after_close: DecimalText
    transaction_cost: DecimalText
    drawdown: DecimalText
    executed_from_prior_signal: str


class Transaction(Strict):
    signal_as_of_session: str
    hypothetical_fill_session: str
    exposure_before: DecimalText
    exposure_after: DecimalText
    turnover_fraction: DecimalText
    cost: DecimalText


class Metrics(Strict):
    cumulative_return: DecimalText
    annualized_return: DecimalText
    max_drawdown: DecimalText
    annualized_volatility: DecimalText
    sharpe: DecimalText | None
    risk_free_rate_annual: DecimalText
    turnover_fraction: DecimalText
    transaction_cost_total: DecimalText
    transactions: int = Field(ge=0, le=2000)
    frictionless_benchmark_cumulative_return: DecimalText
    relative_to_frictionless_benchmark: DecimalText


class BacktestReport(Strict):
    contract: Literal["local-backtest-research-v1"]
    engine_revision: Literal[BACKTEST_REVISION]
    input_sha256: Hash
    output_sha256: Hash
    source_snapshot_sha256: Hash
    model_id: Literal["builtin.sma_crossover"]
    model_version: Literal["1.0.0"]
    source_semantics: Literal["fred_price_index_level"]
    point_in_time_membership: Literal["not_used"]
    signal_timing: Literal["after_observed_close"]
    execution_assumption: Literal["next_observed_session_close"]
    tradeable_instrument: Literal[False]
    research_only: Literal[True]
    no_network: Literal[True]
    no_orders: Literal[True]
    persisted: Literal[False]
    test_sessions: int = Field(ge=2, le=2000)
    rows: list[EquityRow] = Field(min_length=2, max_length=2000)
    transactions: list[Transaction] = Field(max_length=2000)
    metrics: Metrics
    caution: Literal[
        "Hypothetical FRED index-price-level exposure, not a tradable fill or "
        "dividend-inclusive return; benchmark ignores all transaction costs."
    ]


class DurableBacktestResult(Strict):
    contract_version: Literal["backtest-job-result-v1"]
    request_sha256: Hash
    report: BacktestReport
    evaluated_at_utc: datetime


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def checked_result(
    raw: str, request: DurableBacktestRequest, request_hash: str
) -> DurableBacktestResult:
    if len(raw.encode()) > MAX_RESULT_BYTES:
        raise BacktestContractError("Backtest result exceeds the retained result limit")
    result = DurableBacktestResult.model_validate_json(raw)
    report = result.report
    if canonical(json.loads(raw)["report"]) != canonical(report.model_dump(mode="json")):
        raise ValueError("Stored backtest report is not canonical")
    simulation = request.simulation
    if (
        result.evaluated_at_utc.utcoffset() != UTC.utcoffset(None)
        or result.request_sha256 != request_hash
        or report.source_snapshot_sha256 != simulation.expected_snapshot_sha256
        or report.input_sha256
        != digest(
            {
                "backtest_contract": report.contract,
                "engine_revision": report.engine_revision,
                "request": simulation.model_dump(mode="json"),
                "model_result_sha256": request.source_output_sha256,
                "model_lineage_sha256": request.source_lineage_sha256,
            }
        )
        or report.output_sha256 != digest(report.model_dump(mode="json", exclude={"output_sha256"}))
        or report.test_sessions != len(report.rows)
        or report.metrics.transactions != len(report.transactions)
    ):
        raise ValueError("Stored backtest result failed integrity checks")

    preceding = simulation.training_end.isoformat()
    fills = {}
    for index, row in enumerate(report.rows):
        day = row.session_date
        # fromisoformat alone also accepts forms other than YYYY-MM-DD.
        if (
            datetime.fromisoformat(day).date().isoformat() != day
            or day <= preceding
            or row.executed_from_prior_signal != preceding
            or (index == 0 and day != simulation.test_start.isoformat())
        ):
            raise ValueError("Invalid backtest row chronology")
        fills[day] = preceding
        preceding = day
    if preceding != simulation.test_end.isoformat():
        raise ValueError("Invalid backtest end boundary")
    prior_fill = ""
    for transaction in report.transactions:
        fill = transaction.hypothetical_fill_session
        if fill <= prior_fill or transaction.signal_as_of_session != fills.get(fill):
            raise ValueError("Invalid backtest transaction chronology")
        prior_fill = fill
    return result
