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
    research_window_hours: float = 3.0     # evidence (charts, news, dossiers, scans) counts for this long across sessions
    allow_options: bool = True             # style-controlled (Defensive: false)
    allow_spreads: bool = True             # debit verticals (short leg covered in the same order)
    allow_singles: bool = True             # bought calls / puts
    probe_min: int = 50                    # first entry into a name must sit between probe_min and probe_max (enforced)
    probe_max: int = 100
    # portfolio policy (enforced at the risk boundary and shown as book state)
    min_cash_pct: float = 0.25             # settled-cash reserve: entries may not take settled cash below this share of equity
    max_sector_pct: float = 0.50           # stock + options exposure per sector (screener sector)
    max_slow_pct: float = 0.60             # capital in theses slower than slow_horizon_days
    slow_horizon_days: float = 5.0
    min_expected_move_pct: float = 2.0     # return-on-time bar for STOCK entries: ATR% x sqrt(horizon days) must clear this (options are exempt)


LLM_PRESETS = {
    "lmstudio": {"base_url": "http://localhost:1234", "send_reasoning_effort": True, "note": "OpenAI-compatible server built into LM Studio"},
    "ollama": {"base_url": "http://localhost:11434", "send_reasoning_effort": False, "note": "Ollama's OpenAI-compatible endpoint (/v1)"},
    "vllm": {"base_url": "http://localhost:8000", "send_reasoning_effort": False, "note": "vLLM OpenAI-compatible server"},
    "openai": {"base_url": "https://api.openai.com", "send_reasoning_effort": True, "note": "needs LLM_API_KEY"},
    "openrouter": {"base_url": "https://openrouter.ai/api", "send_reasoning_effort": False, "note": "needs LLM_API_KEY; model like 'qwen/qwen3-235b'"},
    "anthropic": {"base_url": "https://api.anthropic.com", "send_reasoning_effort": False, "note": "OpenAI-compatibility layer; needs LLM_API_KEY"},
    "custom": {"base_url": "", "send_reasoning_effort": False, "note": "any /v1/chat/completions server with tool calling"},
}


class LLMSettings(BaseModel):
    provider: str = Field(default_factory=lambda: os.environ.get("LLM_PROVIDER", "lmstudio"))
    base_url: str = Field(default_factory=lambda: os.environ.get("LLM_BASE_URL") or os.environ.get("LMSTUDIO_BASE_URL", "http://localhost:1234"))
    model: str = Field(default_factory=lambda: os.environ.get("LLM_MODEL") or os.environ.get("LMSTUDIO_MODEL", "qwen/qwen3.8-27b"))
    send_reasoning_effort: bool = True     # only OpenAI o-series and LM Studio understand `reasoning_effort`; others may reject it
    reasoning_effort: str = "medium"
    reasoning_by_phase: dict[str, str] = {"reflect": "high", "research": "high", "postmarket": "medium", "premarket": "medium",
                                          "intraday": "medium", "event": "medium", "adhoc": "medium"}
    temperature: float = 0.3
    max_tokens: int = 8192
    max_tool_calls: int = 36
    context_char_budget: int = 190_000    # upper bound; the effective budget is derived from the detected context window (see llm.context_plan)
    context_tokens: int = Field(default_factory=lambda: int(os.environ.get("LLM_CONTEXT_TOKENS", "0") or 0))  # 0 = auto-detect from the server
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
    target_mode: str = "take"              # take: the watcher sells at the target itself, then wakes the agent; wake: the agent decides
    target_take_pct: int = 100             # share of the position the watcher sells at the target (the rest keeps a stop at breakeven or better)
    auto_stops: bool = True                # keep a resting stop order on every position
    expiry_guard: bool = True              # force-close options on expiration day and file do-not-exercise as backstop
    expiry_wake_dte: int = 1               # wake the agent when an option has <= this many days left
    expiry_force_close_time: str = "14:30"  # ET, expiration day: close anything still open
    expiry_dne_time: str = "15:45"          # ET, expiration day: file DNE on any long option still held
    assignment_delta: float = 0.85         # short leg |delta| at/above this -> assignment-risk wake
    study_enabled: bool = True             # off-hours study sessions: learn one curriculum topic, save it to the library
    study_per_night: int = 2               # bounded: at most this many study sessions per night (rolling 12h)
    study_start: str = "20:00"             # ET; window may wrap past midnight
    study_end: str = "06:30"


class LabSettings(BaseModel):
    """Gates for the hypothesis lab (docs/hypothesis-lab-spec.md). Promotion is never automatic."""
    min_events: int = 20               # event-study sample floor
    min_signals: int = 30              # screen-backtest sample floor
    min_edge_pct: float = 0.5          # mean effect over the control at the stated horizon, percentage points
    min_hit_edge_pts: float = 10.0     # hit-rate over the control, points
    worst_mult: float = 3.0            # worst single outcome must be >= -worst_mult x mean
    shadow_min_signals: int = 10
    shadow_min_weeks: int = 4
    option_shadow_min_signals: int = 20
    option_shadow_min_weeks: int = 8
    lab_sessions_per_night: int = 4
    decline_cooldown_days: int = 30


class Settings(BaseModel):
    searxng_url: str = Field(default_factory=lambda: os.environ.get("SEARXNG_URL", "http://127.0.0.1:8088"))
    llm: LLMSettings = LLMSettings()
    risk: RiskLimits = RiskLimits()
    schedule: ScheduleSettings = ScheduleSettings()
    watch: WatchSettings = WatchSettings()
    lab: LabSettings = LabSettings()
    screener_universe_extra: list[str] = []


def _load_toml() -> dict[str, Any]:
    p = ROOT / "munchkin.toml"
    if p.exists():
        with open(p, "rb") as f:
            return tomllib.load(f)
    return {}


LLM_OVERRIDE_FILE = DATA_DIR / "llm.json"


def llm_api_key() -> str:
    """Optional bearer token for hosted providers; read lazily from .env, never exposed to the model."""
    from dotenv import dotenv_values
    vals = dotenv_values(ROOT / ".env") if (ROOT / ".env").exists() else {}
    return (vals.get("LLM_API_KEY") or os.environ.get("LLM_API_KEY") or "").strip()


def load_llm_settings() -> LLMSettings:
    """TOML/env defaults, then dashboard overrides from data/llm.json. Read per session, so changes need no restart."""
    raw = _load_toml().get("llm", {})
    s = LLMSettings(**raw)
    if LLM_OVERRIDE_FILE.exists():
        try:
            import json as _json
            ov = _json.loads(LLM_OVERRIDE_FILE.read_text())
            s = s.model_copy(update={k: v for k, v in ov.items() if k in LLMSettings.model_fields and v not in (None, "")})
        except Exception:
            pass
    return s


def load_settings() -> Settings:
    raw = _load_toml()
    return Settings(**raw)


SETTINGS = load_settings()
