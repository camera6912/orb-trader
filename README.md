# orb-trader

15-minute **Opening Range Breakout (ORB)** bot (paper trading) using Schwab market data.

## Strategy (rules)

All times **US/Eastern**.

1. **Opening range definition (09:30–09:45)**
   - The first 15-minute window (09:30 to 09:45) defines the day’s opening range.
   - `range_high` = highest high in that window.
   - `range_low` = lowest low in that window.

2. **At 09:45: place OCO entries at the range boundaries**
   - Place an **OCO** (one-cancels-other) entry pair:
     - Long entry at `range_high`
     - Short entry at `range_low`
   - When one side triggers, the other is cancelled.

3. **Targets and stops**
   - **Target:** fixed **+20 points** (configurable `target_points`).
     - Long target = `entry + 20`
     - Short target = `entry - 20`
   - **Stop:**
     - Default: **opposite end of the opening range**
       - Long stop = `range_low`
       - Short stop = `range_high`
     - If opening range size **> 20 points**, use the **midpoint** of the range as the stop for both sides.

4. **10:00: breakeven stop rule**
   - At **10:00**, if the trade is in profit, move the stop to **breakeven** (entry price).

5. **16:00: end-of-day exit**
   - At **4:00 PM**, exit any open position (no overnight holds).

6. **Trade filters**
   - Skip **FOMC days**.
   - Skip **gap-fill days** (configurable via `gap_threshold_pct`).

7. **Risk control**
   - **One trade per day maximum.**

## Project layout

- `src/data/schwab.py` — Schwab API integration (copied from `spx-paper-trader`).
- `src/strategy/orb.py` — opening range computation + plan generation.
- `src/trading/paper.py` — lightweight paper execution engine.
- `src/main.py` — runnable orchestration loop.

## Setup

1. Create a venv and install requirements:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Configure secrets:

- `config/secrets.yaml` (already copied locally; **do not commit**)
- `config/schwab_token.json` (already copied locally; **do not commit**)

3. Edit settings:

- `config/settings.yaml`

## Run (paper)

```bash
python -m src.main
```

This will authenticate to Schwab (interactive on first run), compute the opening range, place paper OCO entries, and manage the bracket + breakeven + EOD rules.

## Notes / next steps

- Replace the paper broker with live order routing (Schwab order endpoints) once ready.
- Implement official FOMC calendar filtering.
- Improve “gap-fill day” logic (currently a placeholder helper).
