"""Trading styles: Defensive / Balanced / Aggressive. A style changes the risk envelope, cadence, reasoning effort and
the agent's brief. It never changes the fixed rules below, which are enforced in code in every mode."""
from __future__ import annotations

from typing import Any

from .config import RiskLimits, WatchSettings

FIXED_RULES = [
    "Cash account only: buys use settled cash, never margin, never unsettled proceeds. The engine refuses anything else.",
    "No shorting stock.",
    "Never writes contracts: no naked or credit options. A short option leg is allowed only as the covered leg of a debit vertical opened in the same order, and only in styles that allow spreads.",
    "Expiry guard: no option is held into expiration (wake at 1 DTE, forced close at 14:30 ET on expiration day, do-not-exercise filed).",
    "Position caps, the daily-loss breaker and the kill switch apply in every style.",
    "Every entry needs a dossier, a catalyst grade with a source, a stop and a target; the research gate cannot be bypassed.",
]

STYLES: dict[str, dict[str, Any]] = {
    "defensive": {
        "label": "Defensive",
        "tagline": "Long-only stock swings. No options. Confirmed catalysts only.",
        "risk": {"max_position_pct": 0.25, "max_positions": 4, "max_options_pct": 0.0, "max_daily_loss_pct": 0.08,
                 "size_mult_speculative": 0.0, "size_mult_no_catalyst": 0.0, "allow_options": False, "allow_spreads": False,
                 "allow_singles": False, "probe_min": 50, "probe_max": 75, "research_min_charts": 5},
        "watch": {"min_gap_seconds": 180, "position_move_pct": 4.0},
        "reasoning": "high",
        "summary": ["stock only, no options", "$50–75 probes, 25% per position, 4 positions max", "confirmed catalysts only (speculative and unexplained moves are blocked)",
                    "stops 1.5–2 ATR, horizons of days to weeks", "daily loss breaker at −8%", "sessions 3 minutes apart, reasoning high"],
        "brief": ("STYLE: DEFENSIVE. Long-only stock swings with multi-day horizons. Options are disabled. Only CONFIRMED catalysts "
                  "qualify; speculative or unexplained moves are not entries. Probes $50–75, structural stops 1.5–2 ATR, never chase. "
                  "Prefer quality names above their 200-day average. Being flat is fine here; being reckless is not."),
    },
    "balanced": {
        "label": "Balanced",
        "tagline": "Stock plus defined-risk options. Speculative ideas at half size.",
        "risk": {"max_position_pct": 0.40, "max_positions": 5, "max_options_pct": 0.60, "max_daily_loss_pct": 0.15,
                 "size_mult_speculative": 0.5, "size_mult_no_catalyst": 0.35, "allow_options": True, "allow_spreads": True,
                 "allow_singles": True, "probe_min": 50, "probe_max": 100, "research_min_charts": 4},
        "watch": {"min_gap_seconds": 60, "position_move_pct": 3.0},
        "reasoning": "medium",
        "summary": ["stock, bought calls/puts and debit verticals", "$50–100 probes, 40% per position, 5 positions max", "speculative catalysts at half size, unexplained moves at 0.35",
                    "structural stops; options 7–21 DTE, out by 3 DTE", "daily loss breaker at −15%", "continuous sessions 60 s apart, reasoning medium"],
        "brief": ("STYLE: BALANCED. Stock for slower theses, defined-risk options (bought calls/puts, debit verticals) for fast setups. "
                  "Probes $50–100. Speculative catalysts at half size; unexplained moves rarely. Structural stops; options 7–21 DTE, "
                  "stop at 50% of premium, first target +80–100%."),
    },
    "aggressive": {
        "label": "Aggressive",
        "tagline": "High-risk, high-reward: bought calls and puts on fast setups, bigger probes, faster exits.",
        "risk": {"max_position_pct": 0.50, "max_positions": 6, "max_options_pct": 0.75, "max_daily_loss_pct": 0.20,
                 "size_mult_speculative": 0.75, "size_mult_no_catalyst": 0.5, "allow_options": True, "allow_spreads": True,
                 "allow_singles": True, "probe_min": 100, "probe_max": 150, "research_min_charts": 4},
        "watch": {"min_gap_seconds": 60, "position_move_pct": 2.5},
        "reasoning": "medium",
        "summary": ["bought calls and puts preferred for fast setups; verticals when IV is rich", "$100–150 probes, 50% per position, 6 positions max",
                    "speculative catalysts at 0.75, unexplained moves at 0.5", "tighter stops, faster exits (+60–80% on premium)", "daily loss breaker at −20%",
                    "continuous sessions, events preempt, reasoning medium"],
        "brief": ("STYLE: AGGRESSIVE. Options are the primary instrument for fast setups: bought calls and puts (single contracts) sized "
                  "$100–150, verticals only when IV is rich. Take the probe when a thesis is decent. Faster exits: first target +60–80% "
                  "on premium, cut at 50%. Stock only for slower theses or names without a liquid chain. Still cash-only, still no writing."),
    },
}
DEFAULT_STYLE = "balanced"


def get_style(journal) -> str:
    try:
        s = journal.get("trading_style")
    except Exception:
        s = None
    return s if s in STYLES else DEFAULT_STYLE


def set_style(journal, name: str, source: str = "dashboard") -> str:
    if name not in STYLES:
        raise ValueError(f"unknown style {name}")
    journal.set("trading_style", name)
    journal.add_event("config", f"trading style set to {STYLES[name]['label']} from the {source}")
    return name


def effective_limits(base: RiskLimits, style: str) -> RiskLimits:
    return base.model_copy(update=STYLES.get(style, STYLES[DEFAULT_STYLE])["risk"])


def effective_watch(base: WatchSettings, style: str) -> WatchSettings:
    return base.model_copy(update=STYLES.get(style, STYLES[DEFAULT_STYLE])["watch"])


def style_brief(style: str) -> str:
    return STYLES.get(style, STYLES[DEFAULT_STYLE])["brief"]


def describe() -> dict[str, Any]:
    return {"fixed_rules": FIXED_RULES,
            "styles": {k: {"label": v["label"], "tagline": v["tagline"], "summary": v["summary"], "risk": v["risk"], "watch": v["watch"], "reasoning": v["reasoning"]} for k, v in STYLES.items()}}
