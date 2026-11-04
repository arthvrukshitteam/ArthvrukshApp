"""Turn the comparison table into an answer — with honest error bars.

Two rules govern everything here (MODEL_AUDIT.md §12):

**Compare within a cutoff, never across.** In 2019-2024 nearly every equity
fund did well; in 2007-2012 nearly none did. Pooling all fund-cutoff rows and
correlating would mostly measure which years were good, not whether the model
picks good funds. So every statistic is computed *per cutoff* first, then
averaged across cutoffs (Fama-MacBeth).

**Overlapping forward windows are not independent.** Monthly cutoffs with an
H-year forward horizon overlap ~98%. Naive standard errors are wrong by roughly
sqrt(H*12). Aggregate t-statistics therefore use Newey-West with a lag of
H*12 months, and every table reports the number of genuinely independent
periods alongside the raw row count.

Nothing here changes the model. It only measures it.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Sequence

from . import config as C

#: Metrics tested for predictive power. `blended_wm` is what the app ranks on;
#: `trailing_wm` is the naive baseline the model must beat.
PARAMETERS = [
    ("blended_wm", "higher is better", "MODEL — the app's actual rank metric"),
    ("fqs", "higher is better", "MODEL — headline score"),
    ("median_wm", "higher is better", "component"),
    ("p10_wm", "higher is better", "component"),
    ("success_pct", "higher is better", "component"),
    ("cv", "LOWER is better", "component"),
    ("trailing_wm", "higher is better", "BASELINE — single most recent window"),
]


# ── rank statistics ──────────────────────────────────────────────────────────

def _ranks(xs: Sequence[float]) -> list[float]:
    """Average ranks, ties shared."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    out = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            out[order[k]] = avg
        i = j + 1
    return out


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Spearman rank correlation. None if undefined (n<3 or a constant series)."""
    n = len(xs)
    if n < 3:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def newey_west_se(series: Sequence[float], lag: int) -> float | None:
    """Newey-West standard error of the mean for an autocorrelated series."""
    n = len(series)
    if n < 2:
        return None
    mu = sum(series) / n
    dev = [x - mu for x in series]
    lag = min(lag, n - 1)
    gamma0 = sum(d * d for d in dev) / n
    var = gamma0
    for k in range(1, lag + 1):
        gk = sum(dev[t] * dev[t - k] for t in range(k, n)) / n
        var += 2.0 * (1.0 - k / (lag + 1.0)) * gk
    if var <= 0:
        var = gamma0                      # NW can go negative in small samples
    return math.sqrt(var / n)


@dataclass
class FamaMacBeth:
    metric: str
    direction: str
    role: str
    n_cutoffs: int
    n_observations: int
    mean_rho: float | None
    nw_se: float | None
    t_stat: float | None
    pct_cutoffs_positive: float | None
    independent_periods: float

    def verdict(self) -> str:
        if self.t_stat is None:
            return "insufficient data"
        if self.independent_periods < 3:
            return "not interpretable — too few independent periods"
        if abs(self.t_stat) < 2:
            return "no reliable signal"
        return "signal detected (positive)" if self.t_stat > 0 else "signal detected (INVERTED)"


# ── main analysis ────────────────────────────────────────────────────────────

def _by_cutoff(rows: Sequence[dict]) -> dict[str, list[dict]]:
    out = defaultdict(list)
    for r in rows:
        out[r["cutoff_date"]].append(r)
    return dict(out)


def independent_periods(cutoffs: Sequence[str], horizon: int) -> float:
    """How many non-overlapping forward windows the cutoff span really contains."""
    if len(cutoffs) < 2:
        return float(len(cutoffs))
    lo, hi = min(cutoffs), max(cutoffs)
    months = (int(hi[:4]) - int(lo[:4])) * 12 + int(hi[5:7]) - int(lo[5:7])
    return round(months / (horizon * 12) + 1, 2)


def rank_quality(
    comparison: Sequence[dict], horizon: int = C.ROLLING_PERIOD_YEARS
) -> list[FamaMacBeth]:
    """Does a higher value of each metric predict a better future outcome?

    Correlated against realised wealth multiple, within each cutoff.
    """
    groups = _by_cutoff(comparison)
    out = []
    for metric, direction, role in PARAMETERS:
        rhos, used, n_obs = [], [], 0
        for cutoff, rows in sorted(groups.items()):
            vals = [r.get(metric) for r in rows]
            fut = [r["actual_wm"] for r in rows]
            pairs = [(v, f) for v, f in zip(vals, fut) if v is not None and f is not None]
            if len(pairs) < 5:
                continue
            rho = spearman([p[0] for p in pairs], [p[1] for p in pairs])
            if rho is None:
                continue
            if direction.startswith("LOWER"):
                rho = -rho                # orient so positive always means "works"
            rhos.append(rho)
            used.append(cutoff)
            n_obs += len(pairs)

        if not rhos:
            out.append(FamaMacBeth(metric, direction, role, 0, 0, None, None, None,
                                   None, 0.0))
            continue
        mean = sum(rhos) / len(rhos)
        se = newey_west_se(rhos, horizon * 12)
        out.append(FamaMacBeth(
            metric=metric, direction=direction, role=role,
            n_cutoffs=len(rhos), n_observations=n_obs,
            mean_rho=round(mean, 4),
            nw_se=round(se, 4) if se else None,
            t_stat=round(mean / se, 2) if se and se > 0 else None,
            pct_cutoffs_positive=round(sum(1 for r in rhos if r > 0) / len(rhos), 3),
            independent_periods=independent_periods(used, horizon),
        ))
    return out


def selection_accuracy(
    comparison: Sequence[dict], horizon: int = C.ROLLING_PERIOD_YEARS
) -> dict:
    """Hit rates for the model's picks, against the only fair benchmark: chance.

    A "top 10% hit rate" of 30% sounds good until you notice that picking at
    random gives 10% — and that ranking on last H years alone might give 35%.
    Both comparisons are reported.
    """
    groups = _by_cutoff(comparison)
    res = {}

    for label, keyer in [
        ("model_blended_wm", lambda r: r["predicted_rank"]),
        ("model_fqs", lambda r: r["predicted_rank_fqs"]),
        ("baseline_trailing_wm", None),
    ]:
        top1 = top5 = top10 = top20 = 0
        n_cut = 0
        pct_captured = []

        for cutoff, rows in sorted(groups.items()):
            n = len(rows)
            if n < 5:
                continue
            if keyer is None:
                vals = [(r.get("trailing_wm"), r) for r in rows]
                if any(v is None for v, _ in vals):
                    continue
                ordered = [r for _, r in sorted(vals, key=lambda t: -t[0])]
            else:
                ordered = sorted(rows, key=keyer)

            n_cut += 1
            k5 = max(1, round(n * 0.05))
            k10 = max(1, round(n * 0.10))
            k20 = max(1, round(n * 0.20))

            if ordered[0]["actual_rank"] == 1:
                top1 += 1
            if any(r["actual_rank"] <= k5 for r in ordered[:k5]):
                top5 += 1
            top10 += sum(1 for r in ordered[:k10] if r["actual_top_10_pct"]) / k10
            top20 += sum(1 for r in ordered[:k20] if r["actual_top_20_pct"]) / k20
            pct_captured.append(sum(r["actual_percentile"] for r in ordered[:k10]) / k10)

        if n_cut == 0:
            res[label] = {"n_cutoffs": 0}
            continue
        res[label] = {
            "n_cutoffs": n_cut,
            "picked_the_actual_winner": round(top1 / n_cut, 4),
            "top5pct_pick_landed_top5pct": round(top5 / n_cut, 4),
            "top10pct_hit_rate": round(top10 / n_cut, 4),
            "top20pct_hit_rate": round(top20 / n_cut, 4),
            "mean_percentile_of_top10pct_picks": round(sum(pct_captured) / n_cut, 4),
        }

    n_avg = (sum(len(v) for v in groups.values()) / len(groups)) if groups else 0
    res["baseline_random"] = {
        "picked_the_actual_winner": round(1 / n_avg, 4) if n_avg else None,
        "top10pct_hit_rate": 0.10,
        "top20pct_hit_rate": 0.20,
        "mean_percentile_of_top10pct_picks": 0.50,
        "note": "what pure chance gives — every model number must beat this",
    }
    res["_meta"] = {
        "n_cutoffs": len(groups),
        "mean_funds_per_cutoff": round(n_avg, 1),
        "independent_periods": independent_periods(list(groups), horizon),
    }
    return res


def wealth_accuracy(comparison: Sequence[dict]) -> dict:
    """How close was the predicted wealth to the realised wealth?

    The model's `median_wm` is the median of many overlapping HISTORICAL
    windows; the actual is ONE realised window. A gap is expected and is not by
    itself a defect — the useful question is whether the gap is systematic.
    """
    rows = [r for r in comparison
            if r.get("actual_wm") is not None and r.get("median_wm") is not None]
    if not rows:
        return {"n": 0}
    errs = [r["actual_wm"] - r["median_wm"] for r in rows]
    rel = [e / r["median_wm"] for e, r in zip(errs, rows) if r["median_wm"]]
    within = sum(1 for r in rows if r["actual_wm"] >= r["p10_wm"])
    n = len(rows)
    return {
        "n": n,
        "mean_predicted_wm": round(sum(r["median_wm"] for r in rows) / n, 4),
        "mean_actual_wm": round(sum(r["actual_wm"] for r in rows) / n, 4),
        "mean_error_wm": round(sum(errs) / n, 4),
        "median_error_wm": round(sorted(errs)[n // 2], 4),
        "mean_abs_pct_error": round(sum(abs(x) for x in rel) / len(rel), 4) if rel else None,
        "pct_actual_above_p10": round(within / n, 4),
        "note": ("p10_wm is sold as a downside case; if 'pct_actual_above_p10' is "
                 "far from ~0.90 the downside estimate is miscalibrated"),
    }


def summarise(comparison: Sequence[dict], horizon: int = C.ROLLING_PERIOD_YEARS) -> dict:
    return {
        "rank_quality": [vars(f) | {"verdict": f.verdict()} for f in
                         rank_quality(comparison, horizon)],
        "selection_accuracy": selection_accuracy(comparison, horizon),
        "wealth_accuracy": wealth_accuracy(comparison),
    }


def render(summary: dict) -> str:
    """Human-readable console rendering."""
    L = []
    meta = summary["selection_accuracy"].get("_meta", {})
    ip = meta.get("independent_periods", 0)

    L.append("RANKING QUALITY — does a higher value predict a better future?")
    L.append(f"  (Spearman per cutoff, averaged; Newey-West t-stat; "
             f"{meta.get('n_cutoffs', 0)} cutoffs, ~{ip} independent periods)")
    L.append(f"  {'metric':<14}{'role':<38}{'mean p':>8}{'t':>7}  verdict")
    for r in summary["rank_quality"]:
        rho = f"{r['mean_rho']:+.3f}" if r["mean_rho"] is not None else "  n/a"
        t = f"{r['t_stat']:+.1f}" if r["t_stat"] is not None else "  n/a"
        L.append(f"  {r['metric']:<14}{r['role']:<38}{rho:>8}{t:>7}  {r['verdict']}")

    L.append("")
    L.append("SELECTION ACCURACY — model vs the yardsticks it must beat")
    sa = summary["selection_accuracy"]
    L.append(f"  {'strategy':<24}{'won':>8}{'top10%':>9}{'top20%':>9}")
    for k in ("model_blended_wm", "model_fqs", "baseline_trailing_wm", "baseline_random"):
        d = sa.get(k, {})
        if not d or d.get("n_cutoffs") == 0:
            continue
        w = d.get("picked_the_actual_winner")
        L.append(f"  {k:<24}{(f'{w:.1%}' if w is not None else 'n/a'):>8}"
                 f"{d.get('top10pct_hit_rate', 0):>8.1%}"
                 f"{d.get('top20pct_hit_rate', 0):>9.1%}")

    L.append("")
    L.append("WEALTH ACCURACY")
    wa = summary["wealth_accuracy"]
    if wa.get("n"):
        L.append(f"  predicted median WM {wa['mean_predicted_wm']}  vs  "
                 f"actual {wa['mean_actual_wm']}   (error {wa['mean_error_wm']:+})")
        L.append(f"  actual landed above the P10 downside case "
                 f"{wa['pct_actual_above_p10']:.1%} of the time (target ~90%)")

    if ip < 3:
        L.append("")
        L.append("  !! Only %.1f independent forward periods. Rank comparisons are "
                 "usable;\n     any claim about return LEVELS is not." % ip)
    return "\n".join(L)
