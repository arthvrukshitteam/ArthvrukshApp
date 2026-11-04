"""Backtest CLI.

    python -m backtest.run_backtest --fixture              # 11-fund dev run
    python -m backtest.run_backtest --cross-check          # prove the fast path
    python -m backtest.run_backtest --limit 300            # partial universe
    python -m backtest.run_backtest                        # full universe

Outputs land in backtest_data/results/ as parquet (csv fallback):

    backtest_predictions   every frozen model output
    backtest_actuals       realised outcomes where the future exists
    backtest_comparison    the join — prediction vs reality
    exclusion_report       every fund/cutoff refused, with a reason
    period_summary         per-cutoff counts
    fund_summary           per-fund aggregate
    data_quality_report    gaps, duplicates, splices, missing months
    model_summary.json     config + coverage
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from . import backtest_engine as engine
from . import config as C
from . import data_loader as loader
from . import model_adapter as adapter
from . import point_in_time as pit


def _write(rows: list[dict], name: str, outdir: Path) -> Path | None:
    if not rows:
        print(f"  (no rows for {name})")
        return None
    try:
        import pandas as pd
        df = pd.DataFrame(rows)
        p = outdir / f"{name}.parquet"
        try:
            df.to_parquet(p, index=False)
        except Exception:
            p = outdir / f"{name}.csv"
            df.to_csv(p, index=False)
    except ImportError:
        import csv
        p = outdir / f"{name}.csv"
        with p.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    print(f"  {name:<24} {len(rows):>9,} rows  →  {p.name}")
    return p


def _fund_summary(comparison: list[dict]) -> list[dict]:
    from collections import defaultdict
    agg = defaultdict(list)
    for r in comparison:
        agg[(r["scheme_code"], r["scheme_name"], r["internal_category"])].append(r)
    out = []
    for (code, name, cat), rows in agg.items():
        n = len(rows)
        out.append({
            "scheme_code": code,
            "scheme_name": name,
            "internal_category": cat,
            "n_validated_cutoffs": n,
            "mean_predicted_rank": sum(r["predicted_rank"] for r in rows) / n,
            "mean_actual_rank": sum(r["actual_rank"] for r in rows) / n,
            "mean_rank_difference": sum(r["rank_difference"] for r in rows) / n,
            "mean_blended_wm": sum(r["blended_wm"] for r in rows) / n,
            "mean_fqs": sum(r["fqs"] for r in rows) / n,
            "mean_actual_wm": sum(r["actual_wm"] for r in rows) / n,
            "times_predicted_top_10pct": sum(1 for r in rows if r["predicted_top_10_pct"]),
            "times_actual_top_10pct": sum(1 for r in rows if r["actual_top_10_pct"]),
            "hits_top_10pct": sum(
                1 for r in rows if r["predicted_top_10_pct"] and r["actual_top_10_pct"]
            ),
        })
    return sorted(out, key=lambda r: r["mean_predicted_rank"])


def cross_check(funds, cutoffs, horizon, n_funds: int) -> bool:
    """Prove the fast path equals full recomputation before trusting a run."""
    print(f"\nCross-checking fast path on {min(n_funds, len(funds))} funds "
          f"x {len(cutoffs)} cutoffs …")
    ok_all = True
    for f in funds[:n_funds]:
        ok, msg = adapter.cross_check(f.nav, f.internal_category, horizon, cutoffs)
        flag = "OK  " if ok else "FAIL"
        print(f"  {flag} {f.scheme_code} {f.scheme_name[:44]:<44} {msg}")
        ok_all &= ok
    return ok_all


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fixture", action="store_true",
                    help="use the 11-fund dev fixture instead of nav_store")
    ap.add_argument("--limit", type=int, default=None, help="cap the fund count")
    ap.add_argument("--horizon", type=int, default=C.ROLLING_PERIOD_YEARS)
    ap.add_argument("--validation-horizon", type=int, default=None)
    ap.add_argument("--min-history", type=int, default=None,
                    help="override derived min history (fixture runs only)")
    ap.add_argument("--cutoff-floor", type=str, default=C.CUTOFF_START_FLOOR,
                    help="earliest cutoff, YYYY-MM-DD")
    ap.add_argument("--cross-check", action="store_true",
                    help="verify the fast path, then exit")
    ap.add_argument("--cross-check-funds", type=int, default=5)
    ap.add_argument("--out", type=str, default=None)
    a = ap.parse_args()

    horizon = a.horizon
    vhorizon = a.validation_horizon or horizon
    min_history = a.min_history if a.min_history is not None else C.MIN_HISTORY_YEARS
    outdir = Path(a.out) if a.out else C.OUT_DIR
    outdir.mkdir(parents=True, exist_ok=True)

    print("=" * 74)
    print("AV FQS — point-in-time backtest")
    print("=" * 74)
    print(C.summary())
    if min_history != C.MIN_HISTORY_YEARS:
        print(f"!! min_history overridden to {min_history}y (derived value is "
              f"{C.MIN_HISTORY_YEARS}y) — results are NOT the production rule")

    funds = (loader.load_fixture() if a.fixture
             else loader.load_store(limit=a.limit))
    if a.fixture and a.limit:
        funds = funds[:a.limit]
    if not funds:
        print("no funds loaded")
        return 1

    dmin, dmax = loader.dataset_span(funds)
    print(f"\ndataset  {dmin} → {dmax}   ({len(funds):,} funds)")
    print(f"last complete month-end: {pit.last_complete_month_end(dmax)}")

    floor = date.fromisoformat(a.cutoff_floor) if a.cutoff_floor else None
    cutoffs = sorted({
        c
        for f in funds
        for c in pit.cutoffs_for_fund(f.nav, dmax, min_history, floor)
    })
    if not cutoffs:
        print("\nNo cutoff has any fund with enough history.")
        print(f"Need {min_history}y of history before a cutoff, and "
              f"{vhorizon}y of data after it for validation.")
        return 1
    print(f"cutoffs  {len(cutoffs)}  ({cutoffs[0]} → {cutoffs[-1]})")

    print("\nBuilding rolling windows (once per fund) …")
    funds = engine.prepare(funds, horizon)

    if a.cross_check:
        ok = cross_check(funds, cutoffs, horizon, a.cross_check_funds)
        print("\nfast path VERIFIED" if ok else "\nfast path FAILED — do not run the backtest")
        return 0 if ok else 1

    print(f"\nRunning {len(cutoffs)} cutoffs …")
    import backtest.eligibility as elig
    _orig = elig.assess
    if min_history != C.MIN_HISTORY_YEARS:
        elig.assess = lambda *ar, **kw: _orig(*ar, **{**kw, "min_history_years": min_history})

    res = engine.run(funds, cutoffs, horizon, vhorizon)
    elig.assess = _orig

    comparison = engine.merge_comparison(res["predictions"], res["actuals"])

    print("\nWriting outputs …")
    _write(res["predictions"], "backtest_predictions", outdir)
    _write(res["actuals"], "backtest_actuals", outdir)
    _write(comparison, "backtest_comparison", outdir)
    _write(res["exclusions"], "exclusion_report", outdir)
    _write(res["period_summary"], "period_summary", outdir)
    _write(_fund_summary(comparison), "fund_summary", outdir)
    _write(loader.data_quality_report(funds), "data_quality_report", outdir)

    n_pending = sum(1 for p in res["predictions"] if p["validation_status"] == "PENDING")
    meta = {
        **res["meta"],
        "n_predictions": len(res["predictions"]),
        "n_validated": len(res["actuals"]),
        "n_pending": n_pending,
        "n_exclusions": len(res["exclusions"]),
        "n_comparison_rows": len(comparison),
        "min_history_years_used": min_history,
        "universe": sorted(C.CORE_SUBS) + (["Hybrid"] if C.INCLUDE_BROAD_HYBRID else []),
        "excluded_categories": sorted(C.EXCLUDED_INTERNAL_CATEGORIES),
        "stitch_direction": C.STITCH_DIRECTION,
        "actual_outcome_maths": C.ACTUAL_OUTCOME_MATHS,
    }
    (outdir / "model_summary.json").write_text(json.dumps(meta, indent=2))

    if comparison:
        from . import analytics
        summary = analytics.summarise(comparison, horizon)
        (outdir / "accuracy_summary.json").write_text(json.dumps(summary, indent=2))
        print("\n" + "=" * 74)
        print(analytics.render(summary))

    print("\n" + "=" * 74)
    print(f"cutoffs generated     {meta['n_cutoffs']}")
    print(f"predictions frozen    {meta['n_predictions']:,}")
    print(f"  validated           {meta['n_validated']:,}")
    print(f"  pending             {n_pending:,}")
    print(f"exclusions logged     {meta['n_exclusions']:,}")
    print(f"comparison rows       {meta['n_comparison_rows']:,}")
    print(f"runtime               {meta['runtime_seconds']}s")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
