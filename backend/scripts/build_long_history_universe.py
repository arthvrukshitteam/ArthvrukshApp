"""Find every fund with >= N years of EFFECTIVE history and score it with the model.

Standalone: no MongoDB, no uvicorn, no reindex. Talks only to api.mfapi.in and
reuses the model's own code paths, so results cannot drift from the live app:

  * amfi_client.fetch_fund_list()   — master list, direct plans + IDCW dropped
  * amfi_client.load_analysis_nav() — THE stitching path (curated predecessors,
                                      forward successor chains, and every stitch
                                      gated by fund_lineage.verify_stitch's NAV
                                      return-correlation test)
  * fqs_engine.analyze_fund()       — FQS / blended WM / stability / tiers
  * category_map.classify_fund()    — internal benchmark bucket

Resumability
────────────
`amfi_client.fetch_fund_nav` is monkey-patched with a disk cache, so every
payload lands in `backtest_data/nav_cache/{code}.json`. Ctrl-C and re-run: the
crawl resumes from cache and only fetches what's missing. Stitching still works
because predecessor fetches go through the same cached function.

Why the crawl is unavoidable
────────────────────────────
mfapi.in's master list carries only {schemeCode, schemeName} — no dates. A
fund's earliest NAV is only knowable by fetching /mf/{code}. There is no bulk
history endpoint, so "which funds have 14 years of data" requires one request
per scheme. `--scope equity` skips the ~9.5k obvious debt/liquid schemes by
name, cutting a ~15k-scheme crawl to roughly 2k.

Usage
─────
    cd backend
    python scripts/build_long_history_universe.py                    # 14y, equity scope
    python scripts/build_long_history_universe.py --min-years 12
    python scripts/build_long_history_universe.py --scope all        # include debt
    python scripts/build_long_history_universe.py --codes 130502,140225,113177
    python scripts/build_long_history_universe.py --no-analyze       # list only, skip FQS

Outputs (repo root):
    long_history_universe.json   full records incl. per-horizon model metrics
    long_history_universe.csv    flat table
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

BACKEND = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import amfi_client  # noqa: E402
from category_map import classify_fund, get_display_category  # noqa: E402
from fqs_engine import analyze_fund  # noqa: E402
from fund_lineage import CURATED_SUCCESSORS  # noqa: E402

CACHE_DIR = REPO_ROOT / "backtest_data" / "nav_cache"

# ── Name-based debt/liquid prefilter (only used when --scope equity) ──────────
# High-confidence tokens ONLY. A fund wrongly dropped here is invisible to the
# rest of the run, so anything ambiguous is deliberately left IN and gets
# classified properly after its payload (with the authoritative AMFI
# scheme_category) is fetched.
_DEBT_TOKENS = [
    r"\bliquid\b", r"\bovernight\b", r"\bmoney market\b", r"\btreasury\b",
    r"\bultra short\b", r"\blow duration\b", r"\bshort duration\b",
    r"\bmedium duration\b", r"\bmedium to long\b", r"\blong duration\b",
    r"\bshort term\b", r"\bmedium term\b", r"\blong term bond\b",
    r"\bdynamic bond\b", r"\bcorporate bond\b", r"\bcredit risk\b",
    r"\bbanking and psu\b", r"\bbanking & psu\b", r"\bpsu debt\b",
    r"\bgilt\b", r"\bg-?sec\b", r"\bsdl\b", r"\bfloater\b",
    r"\bfloating rate\b", r"\bbond fund\b", r"\bdebt\b", r"\bincome fund\b",
    r"\bfmp\b", r"\bfixed maturity\b", r"\binterval\b", r"\bcash\b",
    r"\btarget maturity\b", r"\bconstant maturity\b", r"\bdynamic accrual\b",
    r"\bsavings fund\b", r"\bincome plan\b", r"\bliquidity\b",
]
_DEBT_RE = re.compile("|".join(_DEBT_TOKENS), re.IGNORECASE)
# Rescue tokens — equity/hybrid schemes whose names collide with debt tokens.
_NOT_DEBT_RE = re.compile(
    # "equity & debt" / "equity and debt" are aggressive HYBRIDS (e.g. ICICI
    # Prudential Equity & Debt Fund) — the bare \bdebt\b token would drop them.
    r"equity\s*&\s*debt|equity and debt|debt\s*&\s*equity|"
    r"equity savings|equity income|monthly income|hybrid|balanced|"
    r"asset allocat|multi asset|tax sav|elss|children|retirement",
    re.IGNORECASE,
)

_PLAN_TOKENS = [
    r"\bregular\s+plan\b", r"\bregular\b", r"\bdirect\s+plan\b", r"\bdirect\b",
    r"\bgrowth\s+plan\b", r"\bgrowth\s+option\b", r"\bgrowth\b",
    r"\bbonus\s+option\b", r"\bbonus\b", r"\binstitutional\s+plan\b",
    r"\binstitutional\b", r"\bretail\s+plan\b", r"\bretail\b",
    r"\bsuper\s+institutional\b", r"\bplan\b", r"\boption\b",
    r"\bexisting\s+plan\b", r"\bunclaimed\b", r"\bsegregated\s+portfolio\b",
]
_PLAN_RE = re.compile("|".join(_PLAN_TOKENS), re.IGNORECASE)
_PUNCT_RE = re.compile(r"[^a-z0-9]+")
_NON_GROWTH_OPTION_TOKENS = ("bonus", "dividend", "idcw", "payout", "reinvestment")


def looks_like_debt(name: str) -> bool:
    if _NOT_DEBT_RE.search(name or ""):
        return False
    return bool(_DEBT_RE.search(name or ""))


def normalise_fund_name(name: str) -> str:
    s = (name or "").lower()
    s = _PLAN_RE.sub(" ", s)
    s = _PUNCT_RE.sub(" ", s)
    return " ".join(s.split())


def _variant_rank(doc: dict):
    """Prefer plain Growth on the Regular plan; then shortest name, lowest code."""
    name = doc.get("scheme_name") or ""
    lname = name.lower()
    has_non_growth = any(t in lname for t in _NON_GROWTH_OPTION_TOKENS)
    is_growth = "growth" in lname
    option_tier = 0 if (is_growth and not has_non_growth) else (1 if not has_non_growth else 2)
    if "direct" in lname:
        plan_tier = 2
    elif any(t in lname for t in ("institutional", "retail plan", "super")):
        plan_tier = 1
    else:
        plan_tier = 0
    return (option_tier, plan_tier, len(name), name, doc.get("scheme_code") or 0)


def dedupe_variants(docs: List[dict]) -> tuple[List[dict], dict]:
    """One row per economic fund.

    Key = (fund_house, plan/option-agnostic name, earliest_nav_date). The date is
    in the key on purpose: same house + same normalised name but a genuinely
    different start date is NOT the same fund and must not be merged.
    """
    groups: Dict[tuple, List[dict]] = {}
    order: List[tuple] = []
    for d in docs:
        key = ((d.get("fund_house") or "").strip().lower(),
               normalise_fund_name(d.get("scheme_name") or ""),
               d.get("earliest_nav_date"))
        if key not in groups:
            order.append(key)
        groups.setdefault(key, []).append(d)
    kept, dropped = [], {}
    for key in order:
        group = sorted(groups[key], key=_variant_rank)
        if len(group) > 1:
            dropped[group[0]["scheme_code"]] = [
                {"scheme_code": g["scheme_code"], "scheme_name": g["scheme_name"]}
                for g in group[1:]
            ]
        kept.append(group[0])
    return kept, dropped


# ── Disk-cached NAV fetch (monkey-patched into amfi_client) ───────────────────
_real_fetch = amfi_client.fetch_fund_nav
_stats = {"cache_hits": 0, "fetched": 0, "fetch_errors": 0}


def _install_cache(cache_dir: Path):
    cache_dir.mkdir(parents=True, exist_ok=True)

    async def cached_fetch(scheme_code: int) -> Dict[str, Any]:
        path = cache_dir / f"{int(scheme_code)}.json"
        if path.exists():
            try:
                data = json.loads(path.read_text())
                _stats["cache_hits"] += 1
                return data
            except Exception:
                path.unlink(missing_ok=True)  # corrupt cache entry — refetch
        payload = await _real_fetch(scheme_code)
        try:
            path.write_text(json.dumps(payload))
        except Exception:
            pass
        _stats["fetched"] += 1
        return payload

    amfi_client.fetch_fund_nav = cached_fetch


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--min-years", type=float, default=14.0)
    ap.add_argument("--scope", choices=["equity", "all"], default="equity",
                    help="equity = skip obvious debt/liquid schemes by name")
    ap.add_argument("--codes", default=None,
                    help="comma-separated scheme codes — restrict to just these")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--horizons", default="3,5,10,12,15")
    ap.add_argument("--user-years", type=int, default=12,
                    help="rolling period the user picked (drives Cycle Robustness)")
    ap.add_argument("--no-analyze", action="store_true",
                    help="list eligible funds only, skip the FQS model")
    ap.add_argument("--cache-dir", default=str(CACHE_DIR))
    ap.add_argument("--out-dir", default=str(REPO_ROOT))
    ap.add_argument("--limit", type=int, default=None, help="debug: cap schemes scanned")
    args = ap.parse_args()

    horizons = [int(h) for h in args.horizons.split(",") if h.strip()]
    _install_cache(Path(args.cache_dir))

    # ── Universe ─────────────────────────────────────────────────────────────
    if args.codes:
        codes = [int(c) for c in args.codes.replace(" ", "").split(",") if c]
        universe = [{"schemeCode": c, "schemeName": f"(code {c})"} for c in codes]
        print(f"Restricted to {len(universe)} explicit code(s).")
    else:
        print("Fetching master fund list from api.mfapi.in …")
        try:
            funds = await amfi_client.fetch_fund_list()
        except Exception as exc:
            print(f"ERROR: could not fetch the master list — {exc}", file=sys.stderr)
            return 2
        print(f"  {len(funds):,} regular-plan, non-IDCW schemes")
        if args.scope == "equity":
            universe = [f for f in funds if not looks_like_debt(f.get("schemeName") or "")]
            print(f"  {len(funds) - len(universe):,} skipped as debt/liquid by name "
                  f"(--scope all to include)")
        else:
            universe = list(funds)
        print(f"  {len(universe):,} to scan")

    if args.limit:
        universe = universe[: args.limit]
        print(f"  --limit {args.limit} applied")

    # Predecessor codes are fetched as part of stitching; note which we expect.
    print(f"\nCurated predecessor pairs in play: {len(CURATED_SUCCESSORS)} "
          f"({', '.join(f'{p}->{s}' for p, s in CURATED_SUCCESSORS.items())})")
    print("Note: auto-discovered pairs need merger_discovery_log in Mongo and are")
    print("      NOT active in this standalone run — curated stitching only.\n")

    # ── Crawl + score, streaming so NAV series are never all held at once ────
    sem = asyncio.Semaphore(args.concurrency)
    results: List[dict] = []
    errors: List[dict] = []
    scanned = 0
    total = len(universe)
    t0 = time.time()
    lock = asyncio.Lock()

    async def one(fund: Dict[str, Any]) -> None:
        nonlocal scanned
        code = fund.get("schemeCode")
        try:
            code = int(code)
        except (TypeError, ValueError):
            return
        name_hint = fund.get("schemeName") or ""
        async with sem:
            try:
                loaded = await amfi_client.load_analysis_nav(code)
                meta = loaded["meta"]
                nav = loaded["nav_history"]
                if not nav:
                    raise ValueError("no NAV history")

                earliest, latest = nav[0]["date"], nav[-1]["date"]
                span = (latest - earliest).days / 365.25
                name = meta.get("scheme_name") or name_hint
                amfi_cat = meta.get("scheme_category", "") or ""
                category = classify_fund(name, amfi_cat)

                if span >= args.min_years:
                    broad, sub = get_display_category(amfi_cat)
                    own = loaded["original_nav"]
                    rec = {
                        "scheme_code": code,
                        "scheme_name": name,
                        "fund_house": meta.get("fund_house", ""),
                        "scheme_category": amfi_cat,
                        "category": category,
                        "broad_category": broad,
                        "amfi_sub": sub,
                        "history_years": round(span, 2),
                        "earliest_nav_date": earliest.isoformat(),
                        "latest_nav_date": latest.isoformat(),
                        "nav_points": len(nav),
                        # Provenance: own-code vs stitched, so a 14y pass that
                        # depends on a predecessor is auditable.
                        "earliest_nav_date_own_code": own[0]["date"].isoformat() if own else None,
                        "history_years_own_code": (
                            round((own[-1]["date"] - own[0]["date"]).days / 365.25, 2)
                            if own else None
                        ),
                        "predecessor_used": loaded.get("predecessor_used") or [],
                        "successor_used": loaded.get("successor_used"),
                        "wound_up": loaded.get("wound_up"),
                        "history_note": loaded.get("history_note"),
                        "stitched": bool(loaded.get("predecessor_used")),
                    }
                    rec["eligible_only_via_stitch"] = bool(
                        rec["stitched"]
                        and (rec["history_years_own_code"] or 0) < args.min_years
                    )

                    if not args.no_analyze:
                        out = analyze_fund(
                            nav, category, horizons=horizons,
                            include_windows=False, user_years=args.user_years,
                        )
                        rec["headline_fqs"] = out.get("headline_fqs")
                        rec["headline_tier"] = out.get("headline_tier")
                        rec["headline_horizon"] = out.get("headline_horizon")
                        rec["headline_xirr"] = out.get("headline_xirr")
                        rec["cycle_robustness"] = out.get("cycle_robustness")
                        rec["cycle_variant"] = out.get("cycle_variant")
                        rec["horizons"] = [
                            {k: h.get(k) for k in (
                                "horizon", "fqs", "tier", "blended_wm", "median_wm",
                                "p10_wm", "benchmark_wm", "stability", "success_pct",
                                "n_windows", "cv",
                            )}
                            for h in (out.get("horizons") or [])
                        ]
                    async with lock:
                        results.append(rec)
            except Exception as exc:
                errors.append({"scheme_code": code, "scheme_name": name_hint,
                               "error": f"{type(exc).__name__}: {exc}"})
                _stats["fetch_errors"] += 1
            finally:
                async with lock:
                    scanned += 1
                    if scanned % 100 == 0 or scanned == total:
                        el = time.time() - t0
                        rate = scanned / el if el else 0
                        eta = (total - scanned) / rate if rate else 0
                        print(f"  {scanned:>6}/{total}  eligible={len(results):<5} "
                              f"err={len(errors):<4} cache={_stats['cache_hits']:<6} "
                              f"net={_stats['fetched']:<6} {rate:5.1f}/s  "
                              f"eta {eta/60:5.1f}m", flush=True)

    print(f"Scanning {total:,} schemes at concurrency {args.concurrency} …")
    await asyncio.gather(*[one(f) for f in universe])

    # ── Dedupe + sort ────────────────────────────────────────────────────────
    raw_count = len(results)
    deduped, dropped = dedupe_variants(results)
    deduped.sort(key=lambda d: (-(d.get("history_years") or 0), d.get("scheme_name") or ""))

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "criteria": {
            "min_history_years": args.min_years,
            "scope": args.scope,
            "horizons": horizons,
            "user_years": args.user_years,
            "analyzed": not args.no_analyze,
            "stitching": "curated only (no Mongo => no auto-discovered pairs)",
        },
        "run": {
            "schemes_scanned": scanned,
            "eligible_scheme_codes": raw_count,
            "funds_after_dedupe": len(deduped),
            "stitched": sum(1 for d in deduped if d.get("stitched")),
            "eligible_only_via_stitch": sum(
                1 for d in deduped if d.get("eligible_only_via_stitch")),
            "cache_hits": _stats["cache_hits"],
            "network_fetches": _stats["fetched"],
            "errors": len(errors),
            "elapsed_seconds": round(time.time() - t0, 1),
        },
        "funds": deduped,
        "deduped_variants": dropped,
        "errors": errors[:200],
    }

    json_path = out_dir / "long_history_universe.json"
    json_path.write_text(json.dumps(payload, indent=2, default=str))

    csv_cols = [
        "scheme_code", "scheme_name", "fund_house", "category", "broad_category",
        "amfi_sub", "history_years", "earliest_nav_date", "latest_nav_date",
        "nav_points", "stitched", "eligible_only_via_stitch",
        "history_years_own_code", "earliest_nav_date_own_code", "predecessor_used",
        "headline_fqs", "headline_tier", "headline_horizon", "headline_xirr",
        "cycle_robustness",
    ]
    for h in horizons:
        csv_cols += [f"h{h}_fqs", f"h{h}_blended_wm", f"h{h}_n_windows"]

    csv_path = out_dir / "long_history_universe.csv"
    with csv_path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=csv_cols, extrasaction="ignore")
        w.writeheader()
        for d in deduped:
            row = dict(d)
            row["predecessor_used"] = ",".join(str(p) for p in (d.get("predecessor_used") or []))
            for h in (d.get("horizons") or []):
                row[f"h{h['horizon']}_fqs"] = h.get("fqs")
                row[f"h{h['horizon']}_blended_wm"] = h.get("blended_wm")
                row[f"h{h['horizon']}_n_windows"] = h.get("n_windows")
            w.writerow(row)

    # ── Summary ──────────────────────────────────────────────────────────────
    r = payload["run"]
    print(f"\nScanned {r['schemes_scanned']:,} | eligible codes {r['eligible_scheme_codes']:,} "
          f"| funds after dedupe {r['funds_after_dedupe']:,}")
    print(f"Stitched {r['stitched']:,} (of which {r['eligible_only_via_stitch']:,} clear "
          f"{args.min_years}y ONLY because of a stitched predecessor)")
    print(f"Cache hits {r['cache_hits']:,} | network {r['network_fetches']:,} "
          f"| errors {r['errors']:,} | {r['elapsed_seconds']}s")

    by_sub: Dict[str, int] = {}
    for d in deduped:
        k = d.get("amfi_sub") or "—"
        by_sub[k] = by_sub.get(k, 0) + 1
    print("\nBy AMFI sub-category:")
    for sub, n in sorted(by_sub.items(), key=lambda kv: -kv[1]):
        print(f"  {n:5d}  {sub}")

    print(f"\nWrote {json_path}")
    print(f"Wrote {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
