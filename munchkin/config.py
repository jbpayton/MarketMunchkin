"""Configuration and secrets.

Secrets are loaded from .env into this module only. Nothing in here is ever
handed to the LLM: the tool layer only exposes the derived `Settings` object,
which contains no credentials, and every tool result passes through
`redact()` before the model sees it.
"""
from __future__ import annotations

import os
import pathlib
import tomllib
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel, Field

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)
HALT_FILE = DATA_DIR / "HALT"

load_dotenv(ROOT / ".env")


# --------------------------------------------------------------------------- secrets
class _Secrets:
    """Holds credentials. Deliberately not a pydantic model and has a redacted repr."""

    def __init__(self) -> None:
        self.alpaca_key = os.environ.get("ALPACA_API_KEY", "")
        self.alpaca_secret = os.environ.get("ALPACA_SECRET_KEY", "")
        self.alpaca_paper = os.environ.get("ALPACA_PAPER", "true").lower() != "false"

    def __repr__(self) -> str:  # pragma: no cover
        return "<Secrets redacted>"

    __str__ = __repr__


_SECRETS = _Secrets()


def alpaca_credentials() -> tuple[str, str, bool]:
    if not _SECRETS.alpaca_key or not _SECRETS.alpaca_secret:
        raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY missing from .env")
    return _SECRETS.alpaca_key, _SECRETS.alpaca_secret, _SECRETS.alpaca_paper


def redact(text: str) -> str:
    """Scrub any credential string out of text destined for the LLM or logs."""
    if not isinstance(text, str):
        text = str(text)
    for s in (_SECRETS.alpaca_key, _SECRETS.alpaca_secret):
        if s and s in text:
            text = text.replace(s, "[REDACTED]")
    return text


# --------------------------------------------------------------------------- settings
class RiskLimits(BaseModel):
    starting_capital: float = 500.0
    max_position_pct: float = 0.40         # of virtual equity, per position (cost basis)
    max_options_pct: float = 0.60          # total premium at risk across all option positions
    max_positions: int = 5
    max_daily_loss_pct: float = 0.15       # equity drawdown from day start that blocks new entries
    enforce_pdt: bool = True               # false = no day-trade cap (cash account; settled-cash rule still applies)
    max_day_trades_5d: int = 3             # FINRA PDT threshold; the 4th is the violation
    settlement_days: int = 1               # T+1 for stocks and options
    require_settled_cash: bool = True      # never buy with unsettled sale proceeds (avoids GFVs)
    min_stock_price: float = 1.0
    min_avg_dollar_volume: float = 5_000_000
    max_option_spread_pct: float = 0.30    # (ask-bid)/mid
    min_option_open_interest: int = 50
    min_option_dte: int = 3                # entries need >= 3 days to expiry (we never hold into expiration day)
    max_option_dte: int = 120
    max_limit_deviation_pct: float = 0.05  # stock limit price vs last trade sanity band
    allow_market_orders_options: bool = False
    allow_extended_hours: bool = False
    size_mult_speculative: float = 0.5     # per-position cap multiplier when the catalyst is a rumor/unnamed source
    size_mult_no_catalyst: float = 0.35    # ... when there is no identifiable driver (pure technical)
    research_min_charts: int = 4           # candidates that must be charted before any entry


class LLMSettings(BaseModel):
    base_url: str = Field(default_factory=lambda: os.environ.get("LMSTUDIO_BASE_URL", "http://localhost:1234"))
    model: str = Field(default_factory=lambda: os.environ.get("LMSTUDIO_MODEL", "qwen/qwen3.8-27b"))
    reasoning_effort: str = "medium"
    reasoning_by_phase: dict[str, str] = {"reflect": "high", "research": "high", "postmarket": "medium", "premarket": "medium",
                                          "intraday": "medium", "event": "medium", "adhoc": "medium"}
    temperature: float = 0.3
    max_tokens: int = 8192
    max_tool_calls: int = 36
    context_char_budget: int = 190_000    # ~52k tokens; model is loaded at 64k (8k reserved for output)
    tool_result_max_chars: int = 5_000
    timeout_s: float = 900.0


class ScheduleSettings(BaseModel):
    timezone: str = "America/New_York"
    premarket: list[str] = ["08:45"]
    intraday: list[str] = ["09:45", "10:45", "12:00", "13:30", "14:45", "15:35"]
    postmarket: list[str] = ["16:15"]
    research_weekday: int = 5             # Saturday
    research_time: str = "10:00"


class WatchSettings(BaseModel):
    poll_seconds: int = 60                 # watcher cadence during market hours
    continuous: bool = True                # market hours: next session starts min_gap_seconds after the last one ends
    min_gap_seconds: int = 60
    intraday_interval_min: int = 30        # baseline cadence when continuous=false
    offhours_interval_min: int = 30        # off-hours cadence for queued tasks / reflection
    offhours_windows: list[str] = ["06:30-08:40", "16:25-21:30"]   # trading days, ET
    weekend_window: str = "09:00-19:00"
    max_reflect_per_day: int = 8           # free-thinking sessions when the queue is empty
    intraday_start: str = "09:35"
    intraday_end: str = "15:50"
    event_cooldown_min: int = 8            # min gap between sessions (events queue up meanwhile)
    position_move_pct: float = 3.0         # wake on a held name moving this much since the last session
    index_move_pct: float = 0.8            # wake on SPY/QQQ moving this much since the last session
    news_wake: bool = True                 # wake on new headlines for held names
    target_renotify_min: int = 30
    auto_stops: bool = True                # keep a resting stop order on every position
    expiry_guard: bool = True              # force-close options on expiration day and file do-not-exercise as backstop
    expiry_wake_dte: int = 1               # wake the agent when an option has <= this many days left
    expiry_force_close_time: str = "14:30"  # ET, expiration day: close anything still open
    expiry_dne_time: str = "15:45"          # ET, expiration day: file DNE on any long option still held
    assignment_delta: float = 0.85         # short leg |delta| at/above this -> assignment-risk wake


class Settings(BaseModel):
    searxng_url: str = Field(default_factory=lambda: os.environ.get("SEARXNG_URL", "http://127.0.0.1:8088"))
    llm: LLMSettings = LLMSettings()
    risk: RiskLimits = RiskLimits()
    schedule: ScheduleSettings = ScheduleSettings()
    watch: WatchSettings = WatchSettings()
    screener_universe_extra: list[str] = []


def _load_toml() -> dict[str, Any]:
    p = ROOT / "munchkin.toml"
    if p.exists():
        with open(p, "rb") as f:
            return tomllib.load(f)
    return {}


def load_settings() -> Settings:
    raw = _load_toml()
    return Settings(**raw)


SETTINGS = load_settings()
