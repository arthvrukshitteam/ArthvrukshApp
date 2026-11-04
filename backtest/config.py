"""Backtest configuration.

Every model rule here is DERIVED from the live implementation, never duplicated.
Where a constant exists in the model, it is imported. Where it is a backtest
choice, it is defined here and cross-referenced to the decision in MODEL_AUDIT.md.

See MODEL_AUDIT.md Appendix B.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / "backend"

# The model is the source of truth — import it, do not reimplement it.
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from fqs_engine import HORIZONS_FULL, SIP_AMOUNT  # noqa: E402  (model constants)

# ── Derived from the model ───────────────────────────────────────────────────

#: ₹ per instalment. fqs_engine.py:513
SIP_AMOUNT = SIP_AMOUNT

#: Horizons the model supports at all. fqs_engine.py:514
SUPPORTED_HORIZONS = tuple(HORIZONS_FULL)

#: Extra history the app demands beyond the rolling period. server.py:137
#: Imported rather than hardcoded so a change in the app propagates here.
def _history_buffer_years() -> int:
    """Read HISTORY_BUFFER_YEARS out of server.py without importing FastAPI."""
    import re

    src = (BACKEND / "server.py").read_text()
    m = re.search(r"^HISTORY_BUFFER_YEARS\s*=\s*(\d+)", src, re.MULTILINE)
    if not m:
        raise RuntimeError(
            "HISTORY_BUFFER_YEARS not found in server.py — the eligibility rule "
            "moved. Re-audit before running the backtest."
        )
    return int(m.group(1))


HISTORY_BUFFER_YEARS = _history_buffer_years()

# ── Backtest choices (MODEL_AUDIT.md §14) ────────────────────────────────────

#: Rolling period under test. The model supports 1..20; this is the one we run.
ROLLING_PERIOD_YEARS = 5

#: Forward validation horizon. Equal to the rolling period by construction:
#: the model's prediction is about H-year SIP outcomes, so the realised
#: outcome must also be an H-year SIP.
VALIDATION_HORIZON_YEARS = ROLLING_PERIOD_YEARS

#: D-derived, never hardcoded. MODEL_AUDIT.md §4.
MIN_HISTORY_YEARS = ROLLING_PERIOD_YEARS + HISTORY_BUFFER_YEARS

INVESTMENT_MODE = "sip"

#: D5 — cutoffs begin as early as each fund's own history allows, rather than a
#: fixed calendar date, to maximise the number of independent forward periods.
#: Set to a date string to force a floor (e.g. "2019-01-31").
CUTOFF_START_FLOOR: str | None = None

#: D4 — the app sorts the user-facing universe by this. server.py:548-557
PRIMARY_RANK_METRIC = "blended_wm"
SECONDARY_RANK_METRIC = "fqs"

#: D3 — the sub-categories the home screen's default view actually shows.
#: Mirrors frontend/app/(tabs)/home.tsx:166-174 (CORE_SUBS) plus Hybrid.
CORE_SUBS = frozenset({
    "Multi Cap Fund",
    "Large Cap Fund",
    "Large & Mid Cap Fund",
    "Mid Cap Fund",
    "Small Cap Fund",
    "ELSS",
    "Flexi Cap Fund",
})
INCLUDE_BROAD_HYBRID = True

#: D2 — Debt funds are force-ranked last by the app and are never realistically
#: selectable, so they are out of scope. Consequence: the backtest cannot
#: evaluate whether the Debt demotion rule is correct.
EXCLUDED_INTERNAL_CATEGORIES = frozenset({"Debt", "Gold/Silver"})

#: D7 — forward (successor) stitching is look-ahead. MODEL_AUDIT.md §3.1
STITCH_DIRECTION = "backward_only"

#: D1 — the realised outcome is computed with the model's own window machinery,
#: defect included, so prediction and actual share a scale. MODEL_AUDIT.md §5.2
ACTUAL_OUTCOME_MATHS = "model_identical"

# ── Paths ────────────────────────────────────────────────────────────────────

DATA_DIR = REPO_ROOT / "backtest_data"
NAV_STORE = DATA_DIR / "nav_store"          # one .json per scheme, resumable
NAV_PARQUET = DATA_DIR / "nav_store.parquet"  # consolidated, fast path
UNIVERSE_FILE = DATA_DIR / "universe.json"   # scheme_code -> metadata
FIXTURE_NAV = DATA_DIR / "universe_nav.json"  # 11-fund dev fixture

OUT_DIR = DATA_DIR / "results"
CHECKPOINT_DIR = OUT_DIR / "_checkpoints"

# ── Runtime ──────────────────────────────────────────────────────────────────

DOWNLOAD_CONCURRENCY = 12      # be a good citizen with a free public API
DOWNLOAD_RETRIES = 3
DOWNLOAD_TIMEOUT_S = 45

#: Write a checkpoint every N cutoffs so a long run is resumable.
CHECKPOINT_EVERY_CUTOFFS = 6


def summary() -> str:
    return "\n".join([
        f"mode                 {INVESTMENT_MODE}",
        f"sip_amount           {SIP_AMOUNT:,.0f}",
        f"rolling_period       {ROLLING_PERIOD_YEARS}y",
        f"history_buffer       {HISTORY_BUFFER_YEARS}y   (from server.py)",
        f"min_history          {MIN_HISTORY_YEARS}y   (derived)",
        f"validation_horizon   {VALIDATION_HORIZON_YEARS}y",
        f"primary_rank         {PRIMARY_RANK_METRIC}",
        f"secondary_rank       {SECONDARY_RANK_METRIC}",
        f"stitching            {STITCH_DIRECTION}",
        f"actual_outcome       {ACTUAL_OUTCOME_MATHS}",
        f"excluded_categories  {sorted(EXCLUDED_INTERNAL_CATEGORIES)}",
        f"cutoff_floor         {CUTOFF_START_FLOOR or 'per-fund earliest feasible'}",
    ])
