from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.utcnow()


# ── Requests ──────────────────────────────────────────────


class SignalIn(BaseModel):
    id: str = Field(default_factory=_uuid)
    source: str
    ticker: str
    direction: str
    signal_score: float = 0.0
    indicators: Any = None
    proposed: bool = False
    traded: bool = False
    skip_reason: Optional[str] = None
    ts: Optional[datetime] = None

    @field_validator("indicators", mode="before")
    @classmethod
    def _dump_indicators(cls, v):
        if v is not None and not isinstance(v, str):
            return json.dumps(v)
        return v


class EntryIn(BaseModel):
    id: str = Field(default_factory=_uuid)
    signal_id: Optional[str] = None
    broker: str
    ticker: str
    instrument: str
    direction: str
    strike: Optional[float] = None
    expiry: Optional[str] = None
    contracts: int = 1
    entry_price: float
    total_cost: Optional[float] = None
    account_size_at_entry: Optional[float] = None
    ts: Optional[datetime] = None

    def model_post_init(self, __context):
        if self.total_cost is None:
            self.total_cost = self.entry_price * self.contracts
        if self.account_size_at_entry and self.total_cost:
            self.pct_of_account = round(
                (self.total_cost / self.account_size_at_entry) * 100, 2
            )
        else:
            self.pct_of_account = None

    pct_of_account: Optional[float] = None


class ExitIn(BaseModel):
    id: str = Field(default_factory=_uuid)
    entry_id: str
    exit_price: float
    contracts: int = 1
    pnl: Optional[float] = None
    pnl_pct: Optional[float] = None
    exit_reason: Optional[str] = None
    hold_time_min: Optional[int] = None
    ts: Optional[datetime] = None


# ── Responses ─────────────────────────────────────────────


class TradeOut(BaseModel):
    id: str
    signal_id: Optional[str] = None
    ts: Optional[str] = None
    broker: Optional[str] = None
    ticker: Optional[str] = None
    instrument: Optional[str] = None
    direction: Optional[str] = None
    strike: Optional[float] = None
    expiry: Optional[str] = None
    contracts: Optional[int] = None
    entry_price: Optional[float] = None
    total_cost: Optional[float] = None
    account_size_at_entry: Optional[float] = None
    pct_of_account: Optional[float] = None
    exits: list[dict] = []


class StatsOut(BaseModel):
    total_trades: int = 0
    win_rate: float = 0.0
    avg_pnl: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    best_trade: float = 0.0
    worst_trade: float = 0.0
    total_pnl: float = 0.0
    by_ticker: dict = {}
    by_broker: dict = {}


class DailyOut(BaseModel):
    date: str
    trades: int
    wins: int
    losses: int
    pnl: float


class HealthOut(BaseModel):
    ok: bool = True
    db_path: str
    signal_count: int
    entry_count: int
    exit_count: int
