"""Point-in-time data construction — the module that makes the backtest honest.

Everything the model is allowed to see at cutoff T passes through here. If a
future NAV reaches the model, the bug is in this file.

Three responsibilities:

1. **Truncation.** Slice a fund's raw daily NAV series to `date <= cutoff`, then
   run the model's own `sanitize_nav_history` on the *truncated copy*
   (MODEL_AUDIT.md L1 — sanitisation rebases earlier NAVs from a later-detected
   split, so running it once on full history would leak the future backwards).

2. **Cutoff generation.** Cutoffs are true calendar month-ends only
   (MODEL_AUDIT.md §2.2), and never include the dataset's final partial month.

3. **The nesting fast path.** Rolling windows on a truncated series are a
   *prefix* of the full-history windows, so each fund's windows can be built
   once and sliced per cutoff instead of recomputed per cutoff. The admission
   rule is subtler than it looks — see `admissible_count()` — and is checked by
   `verify_nesting()` rather than assumed.

   MODEL_AUDIT.md §7.2 originally stated the filter as `end_date <= cutoff`.
   **That is wrong** and this module does not use it. The model terminates on
   `EOMONTH(B_k) <= EOMONTH(last month-end)`, where `B_k` is the window's
   *nominal* redemption date — which can fall in the month **after** the month
   the end NAV is actually read from (`compute_rolling_windows` takes the last
   month-end at or before `B_k`). A window can therefore have
   `end_date <= cutoff` and still be inadmissible at that cutoff, because the
   model insists on seeing the nominal redemption month before it will count
   the window at all. Using the naive filter would hand the model one extra
   window at boundary cutoffs — a genuine, if small, look-ahead.
"""
from __future__ import annotations

import calendar
import copy
from datetime import date, timedelta
from typing import Iterable, Sequence

from . import config as C

import sys
sys.path.insert(0, str(C.BACKEND))
from amfi_client import (  # noqa: E402
    SPLICE_MAX_GAP_DAYS,
    SPLICE_RATIO_THRESHOLD,
    sanitize_nav_history,
)

NavRow = dict          # {"date": date, "nav": float}
NavSeries = list[NavRow]


# ── truncation ───────────────────────────────────────────────────────────────

def truncate(raw: Sequence[tuple[date, float]], cutoff: date) -> NavSeries:
    """Raw (date, nav) pairs → the model's NAV format, restricted to <= cutoff.

    The returned rows are freshly allocated because `sanitize_nav_history`
    mutates its input in place (amfi_client.py:209-210). Sharing rows between
    cutoffs would let one cutoff's rebasing contaminate another's.
    """
    rows = [{"date": d, "nav": float(n)} for d, n in raw if d <= cutoff]
    if len(rows) < 2:
        return rows
    return sanitize_nav_history(rows)


def truncate_unsanitised(raw: Sequence[tuple[date, float]], cutoff: date) -> NavSeries:
    """Truncate without splice adjustment — for diagnostics only."""
    return [{"date": d, "nav": float(n)} for d, n in raw if d <= cutoff]


# ── splice detection ─────────────────────────────────────────────────────────

def splice_dates(raw: Sequence[tuple[date, float]]) -> list[date]:
    """Dates at which `sanitize_nav_history` would rebase the earlier segment.

    Reproduces amfi_client.py:196-210 read-only. A fund with no splice dates is
    unaffected by sanitisation, which simplifies reasoning about the fast path.
    """
    out: list[date] = []
    for i in range(1, len(raw)):
        (pd_, pn), (cd, cn) = raw[i - 1], raw[i]
        if pn <= 0 or cn <= 0:
            continue
        if (cd - pd_).days > SPLICE_MAX_GAP_DAYS:
            continue
        ratio = cn / pn
        if ratio >= SPLICE_RATIO_THRESHOLD or ratio <= 1.0 / SPLICE_RATIO_THRESHOLD:
            out.append(cd)
    return out


# ── cutoff generation ────────────────────────────────────────────────────────

def _month_end(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def _next_month_end(d: date) -> date:
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return date(y, m, calendar.monthrange(y, m)[1])


def month_end_range(first: date, last: date) -> list[date]:
    """Every calendar month-end in [first, last], inclusive."""
    out, cur = [], _month_end(first)
    if cur < first:
        cur = _next_month_end(cur)
    while cur <= last:
        out.append(cur)
        cur = _next_month_end(cur)
    return out


def last_complete_month_end(dataset_max: date) -> date:
    """The final cutoff the dataset can support.

    The dataset's own last month is partial — its "month-end" NAV is simply the
    last row present, not a real month-end (MODEL_AUDIT.md §2.2). Including it
    would make that month's NAV depend on when the data was downloaded.
    """
    first_of_month = date(dataset_max.year, dataset_max.month, 1)
    return first_of_month - timedelta(days=1)


def cutoffs_for_fund(
    raw: Sequence[tuple[date, float]],
    dataset_max: date,
    min_history_years: int = C.MIN_HISTORY_YEARS,
    floor: date | None = None,
) -> list[date]:
    """Every cutoff at which this fund could possibly be eligible.

    Earliest cutoff is the first month-end at which the fund has
    `min_history_years` of history (D5). Eligibility is re-checked properly in
    `eligibility.py` against actual monthly observations; this is only a cheap
    bound so we do not evaluate obviously-impossible cutoffs.
    """
    if not raw:
        return []
    inception = raw[0][0]
    earliest = date(
        inception.year + min_history_years,
        inception.month,
        min(inception.day, calendar.monthrange(inception.year + min_history_years,
                                               inception.month)[1]),
    )
    if floor and floor > earliest:
        earliest = floor
    latest = min(last_complete_month_end(dataset_max), _month_end(raw[-1][0]))
    if earliest > latest:
        return []
    return month_end_range(earliest, latest)


def global_cutoffs(
    dataset_min: date, dataset_max: date, floor: date | None = None
) -> list[date]:
    """The union of cutoffs across the whole dataset, for reporting."""
    start = floor or dataset_min
    return month_end_range(start, last_complete_month_end(dataset_max))


# ── nesting fast path ────────────────────────────────────────────────────────

def min_wm_required(category: str, horizon: int) -> float:
    """The model's hurdle for this category/horizon. MODEL_AUDIT.md §6.6."""
    from category_map import CATEGORY_TABLE, DEFAULT_CATEGORY

    cat = CATEGORY_TABLE.get(category) or CATEGORY_TABLE[DEFAULT_CATEGORY]
    anchored = cat["wm"].get(horizon)
    if anchored is not None:
        return anchored
    m = cat["xirr"] / 12.0
    n = horizon * 12
    return ((1 + m) ** n - 1) / (n * m) if m else 1.0


def build_all_windows(
    raw: Sequence[tuple[date, float]], category: str, horizon: int
) -> tuple[list, list[date]]:
    """Full-history rolling windows plus each window's nominal redemption month.

    Calls the model's own `compute_rolling_windows` once, then replays that
    function's loop step-for-step to label each *emitted* window with its own
    `B_k`. Matching by `start_date` is not safe: a data gap makes consecutive
    windows share a first SIP date (observed 6 times in a single fixture fund),
    which silently mis-assigns every `B_k` after the collision. The replay is
    asserted against the engine's real output, so any drift fails loudly rather
    than corrupting the backtest.
    """
    from category_map import DEFAULT_CATEGORY, max_plausible_wm
    from fqs_engine import (
        SIP_AMOUNT,
        _edate,
        _eomonth,
        compute_rolling_windows,
        extract_month_end_navs,
    )

    monthly = extract_month_end_navs(truncate(raw, date.max))
    if not monthly:
        return [], []
    min_req = min_wm_required(category, horizon)
    windows = compute_rolling_windows(monthly, horizon, min_req, category)
    if not windows:
        return [], []

    # ── replay of compute_rolling_windows (fqs_engine.py:577-634) ────────────
    wm_cap = max_plausible_wm(category or DEFAULT_CATEGORY, horizon)
    n_months = horizon * 12
    invested = SIP_AMOUNT * n_months
    dates = [m["date"] for m in monthly]
    last_eom = _eomonth(monthly[-1]["date"], 0)
    a = _eomonth(monthly[0]["date"], 0) + timedelta(days=1)
    b_eoms: list[date] = []

    while True:
        b = _edate(a, 12 * horizon)
        if _eomonth(b, 0) > last_eom:
            break
        sip_idx = [i for i, d in enumerate(dates) if a <= d < b]
        if not sip_idx:
            break
        end_candidates = [i for i, d in enumerate(dates) if d <= b]
        if not end_candidates:
            a = _eomonth(a, 1)
            continue
        units = sum(SIP_AMOUNT / monthly[i]["nav"] for i in sip_idx)
        wm = units * monthly[end_candidates[-1]]["nav"] / invested
        if wm > wm_cap:                       # MODEL_AUDIT.md §5.3
            a = _eomonth(a, 1)
            continue
        b_eoms.append(_eomonth(b, 0))
        a = _eomonth(a, 1)

    if len(b_eoms) != len(windows):
        raise AssertionError(
            f"window replay drifted from fqs_engine: replay={len(b_eoms)} "
            f"engine={len(windows)}. compute_rolling_windows has changed — "
            "re-audit MODEL_AUDIT.md §5.1 before trusting any backtest output."
        )
    return windows, b_eoms


def admissible_count(b_eoms: Sequence[date | None], last_month_end: date) -> int:
    """How many full-history windows the model would have had at this cutoff.

    Admission rule, straight from `compute_rolling_windows`' loop condition:
    a window exists iff `EOMONTH(B_k) <= EOMONTH(last available month-end)`.
    Windows are generated in increasing `B_k`, so the admissible set is a
    prefix.
    """
    n = 0
    for b in b_eoms:
        if b is None or b > last_month_end:
            break
        n += 1
    return n


def eomonth(d: date) -> date:
    """Excel EOMONTH(d, 0), via the model's own helper."""
    from fqs_engine import _eomonth

    return _eomonth(d, 0)


def month_ends_at(
    raw: Sequence[tuple[date, float]], cutoff: date
) -> list[tuple[date, float]]:
    """The model's month-end NAV series as it stood at `cutoff`."""
    from fqs_engine import extract_month_end_navs

    return [(m["date"], m["nav"]) for m in extract_month_end_navs(truncate(raw, cutoff))]


def last_month_end_at(raw: Sequence[tuple[date, float]], cutoff: date) -> date | None:
    """EOMONTH of the fund's last month-end NAV at or before `cutoff`."""
    monthly = month_ends_at(raw, cutoff)
    return eomonth(monthly[-1][0]) if monthly else None


def windows_at(
    raw: Sequence[tuple[date, float]],
    category: str,
    horizon: int,
    cutoff: date,
) -> tuple[list, float]:
    """Recompute this fund's rolling windows directly on truncated data.

    The slow, unambiguous path. `build_all_windows` + `admissible_count` is the
    fast equivalent and is what the engine actually uses; this exists so the two
    can be cross-checked (`verify_nesting`) and as a fallback.
    """
    from fqs_engine import compute_rolling_windows, extract_month_end_navs

    min_req = min_wm_required(category, horizon)
    monthly = extract_month_end_navs(truncate(raw, cutoff))
    return compute_rolling_windows(monthly, horizon, min_req, category), min_req


def verify_nesting(
    raw: Sequence[tuple[date, float]],
    category: str,
    horizon: int,
    cutoffs: Iterable[date],
    tol: float = 1e-9,
) -> tuple[bool, str]:
    """Prove, for THIS fund, that slice-of-full equals recompute-on-truncated.

    Splices rebase a prefix of the series by a constant, and a rolling SIP
    window's wealth multiple is invariant to uniform scaling of every NAV inside
    it — so the property should survive sanitisation. "Should" is not "does",
    hence this check runs over every cutoff, not a sample.
    """
    from fqs_engine import compute_rolling_windows, extract_month_end_navs

    min_req = min_wm_required(category, horizon)
    full, b_eoms = build_all_windows(raw, category, horizon)

    for cutoff in cutoffs:
        direct = compute_rolling_windows(
            extract_month_end_navs(truncate(raw, cutoff)), horizon, min_req, category
        )
        lme = last_month_end_at(raw, cutoff)
        sliced = full[: admissible_count(b_eoms, lme)] if lme else []

        if len(direct) != len(sliced):
            return False, (
                f"window count differs at {cutoff}: "
                f"recomputed={len(direct)} sliced={len(sliced)}"
            )
        for x, y in zip(direct, sliced):
            if x.start_date != y.start_date or x.end_date != y.end_date:
                return False, (
                    f"window bounds differ at {cutoff}: "
                    f"{x.start_date}..{x.end_date} vs {y.start_date}..{y.end_date}"
                )
            if abs(x._wm_raw - y._wm_raw) > tol:
                return False, (
                    f"WM differs at {cutoff} window {x.start_date}: "
                    f"recomputed={x._wm_raw!r} sliced={y._wm_raw!r}"
                )
    return True, "nesting holds under the EOMONTH(B_k) admission rule"


def describe(raw: Sequence[tuple[date, float]]) -> dict:
    """Data-quality facts about one fund's raw series (MODEL_AUDIT.md §10)."""
    if not raw:
        return {"n_points": 0}
    dates = [d for d, _ in raw]
    navs = [n for _, n in raw]
    dupes = len(dates) - len(set(dates))
    gaps = [
        (dates[i - 1].isoformat(), dates[i].isoformat(), (dates[i] - dates[i - 1]).days)
        for i in range(1, len(dates))
        if (dates[i] - dates[i - 1]).days > 45
    ]
    months = {(d.year, d.month) for d in dates}
    span_months = (
        (dates[-1].year - dates[0].year) * 12 + dates[-1].month - dates[0].month + 1
    )
    return {
        "n_points": len(raw),
        "first": dates[0].isoformat(),
        "last": dates[-1].isoformat(),
        "span_years": round((dates[-1] - dates[0]).days / 365.25, 2),
        "duplicate_dates": dupes,
        "months_present": len(months),
        "months_expected": span_months,
        "missing_months": span_months - len(months),
        "long_gaps": gaps[:20],
        "n_long_gaps": len(gaps),
        "splices": [d.isoformat() for d in splice_dates(raw)],
        "min_nav": min(navs),
        "max_nav": max(navs),
    }
