# Guardrails and rules

Every order the agent wants to place passes through the same layers, in this order. The agent cannot skip a layer,
and the operator's controls sit outside the loop entirely. Rendered copy: `docs/guardrails.png`.

```mermaid
flowchart TD
    A["Agent decides to enter<br/>(buy_stock · buy_option · open_spread · arm_entry)"] --> RG

    subgraph RG["1 · Research gate  (munchkin/research.py)"]
        direction LR
        R1["market context read"] --- R2["broad scan run"] --- R3["≥ N candidates charted"] --- R4["name grounded in news"] --- R5["dossier exists"]
    end
    RG -->|evidence counts for 3 h across runs| FR

    subgraph FR["2 · Fixed rules — every style, never overridable  (munchkin/styles.py · risk.py)"]
        direction LR
        F1["cash account only<br/>settled funds, never margin"] --- F2["no shorting stock"] --- F3["never writes contracts<br/>no naked or credit options"] --- F4["no option held into expiry"] --- F5["kill switch & daily-loss breaker apply"]
    end
    FR --> ST

    subgraph ST["3 · Style envelope — Defensive · Balanced · Aggressive  (munchkin/styles.py)"]
        direction LR
        S1["per-position cap<br/>25% · 40% · 50%"] --- S2["positions<br/>4 · 5 · 6"] --- S3["instruments<br/>stock only · +bought options · options first"] --- S4["catalyst grade × size<br/>confirmed 1.0 · speculative · none"] --- S5["probe first<br/>$50–75 · $50–100 · $100–150"]
    end
    ST --> PP

    subgraph PP["4 · Portfolio policy  (munchkin/risk.py · book.py)"]
        direction LR
        P1["settled-cash reserve<br/>30% · 25% · 20%"] --- P2["sector cap 50%"] --- P3["slow-thesis cap<br/>none · 60% · 40%"] --- P4["return-on-time bar<br/>ATR × √days ≥ 0 · 2% · 3%<br/>(options exempt)"]
    end
    PP --> OC

    subgraph OC["5 · Order checks  (munchkin/risk.py)"]
        direction LR
        O1["settled cash minus<br/>live reservations"] --- O2["per-underlying cap<br/>stock + options"] --- O3["liquidity & price floors"] --- O4["option quality<br/>DTE · OI · spread · delta"] --- O5["limit within 3% of last<br/>market hours"]
    end
    OC --> RS["6 · Atomic cash reservation<br/>(journal, immediate transaction)"]
    RS --> BR["Broker · Alpaca<br/>paper by default"]
    BR --> WP

    subgraph WP["7 · Protection after the fill  (munchkin/watch.py · exits.py)"]
        direction LR
        W1["stop rests at the broker<br/>GTC whole shares · DAY re-armed"] --- W2["target taken mechanically<br/>then the agent is woken"] --- W3["expiry guard<br/>close 14:30 · DNE 15:45"] --- W4["horizon elapsed → decide"] --- W5["never fires on a fallback quote"]
    end

    subgraph OP["Operator controls  (dashboard · Telegram · CLI)"]
        direction LR
        K1["kill switch<br/>two-step confirm"] --- K2["style switch"] --- K3["promote · demote<br/>strategies, skills, experiments"] --- K4["budgets"]
    end
    OP -.->|"outside the agent's reach"| FR

    subgraph DG["Degradation  (munchkin/market.py · broker.py)"]
        direction LR
        D1["clock fallback"] --- D2["quotes fall back to yfinance,<br/>tagged, never traded on"] --- D3["data-feed circuit breaker"] --- D4["stale dashboard flagged"]
    end
    DG -.-> WP

    classDef fixed fill:#35201f,stroke:#cf5858,color:#f2e9e4
    classDef human fill:#0f2a33,stroke:#5fd3f0,color:#e6f7fb
    class F1,F2,F3,F4,F5 fixed
    class K1,K2,K3,K4 human
```

## The learning loop has its own gates

Claims, skills and strategies are separate objects with separate lifecycles; nothing in them can loosen a layer above.

```mermaid
flowchart LR
    H["Hypothesis<br/>(operator or agent)"] --> SP["Spec validated<br/>trigger · universe · horizon · effect"]
    SP --> T["Test with a control<br/>event study · screen backtest · custom"]
    T -->|pass| TD["Tested"]
    T -->|inconclusive ×2 or fail| RJ["Rejected, kept with its numbers"]
    TD --> SH["Shadow run<br/>virtual fills, no orders"]
    SH --> IN["Decision inbox"]
    IN -->|"operator promotes"| LV["Strategy: skill + detector + budget + kill rules"]
    LV -->|"kill rule trips → recommendation"| IN
    IN -->|"operator demotes"| RT["Paused / retired"]
    EX["Intraday experiment<br/>shadow-only, versioned"] --> IN
    SK["Agent-written skill draft"] -->|"operator approves"| AS["Active skill"]
    classDef human fill:#0f2a33,stroke:#5fd3f0,color:#e6f7fb
    class IN human
```

## Where each rule lives

| rule | enforced in | changeable by |
|---|---|---|
| cash only, no margin, no shorting, never writes contracts, no option into expiry | `risk.py`, `optentry.py`, `exits.py` | nobody at runtime |
| kill switch, daily-loss breaker | `risk.py`, HALT file | operator (switch), style (threshold) |
| position caps, positions, instruments, catalyst multipliers, probes | `styles.py` → `risk.py` | operator via style switch |
| cash reserve, sector cap, slow-thesis cap, return-on-time | `styles.py` → `risk.policy_checks` | operator via style switch |
| research gate | `research.py` | `munchkin.toml [risk]` |
| settled cash minus reservations, per-underlying cap, liquidity, option quality | `risk.py`, `journal.reserve` | `munchkin.toml [risk]` |
| stops, mechanical targets, expiry guard, horizon events, no fires on fallback quotes | `watch.py`, `exits.py` | `munchkin.toml [watch]` |
| degradation fallbacks | `market.py`, `broker.py`, `web.py` | none needed |
| skills, strategies, experiments | `skills.py`, `lab.py`, `experiments.py` | operator approval only |
