"""Load NAV data into `Fund` objects.

Two sources, same output:

  * `backtest_data/nav_store/*.json` — the real universe, written by
    `download_navs.py` on the developer machine.
  * `backtest_data/universe_nav.json` — an 11-fund development fixture used to
    build and test the engine while the download runs.

NAVs are loaded **raw and unsanitised**. Splice adjustment is deliberately NOT
applied here: it must run per cutoff on truncated data, or a split detected in
2023 rewrites the NAVs of 2015 (MODEL_AUDIT.md L1). `point_in_time.truncate`
owns that.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from . import config as C
from .backtest_engine import Fund

import sys
sys.path.insert(0, str(C.BACKEND))
from category_map import classify_fund, get_display_category  # noqa: E402


#: Fixture metadata — the fixture file carries codes only, no names/categories.
FIXTURE_META = {
    "130502": ("HDFC Small Cap Fund - Growth",            "Equity Scheme - Small Cap Fund"),
    "107578": ("Mirae Asset Large Cap Fund - Growth",     "Equity Scheme - Large Cap Fund"),
    "104481": ("Franklin India Prima Fund - Growth",      "Equity Scheme - Mid Cap Fund"),
    "140225": ("Edelweiss Mid Cap Fund - Growth",         "Equity Scheme - Mid Cap Fund"),
    "125494": ("SBI Small Cap Fund - Growth",             "Equity Scheme - Small Cap Fund"),
    "108466": ("ICICI Prudential Large Cap Fund - Growth", "Equity Scheme - Large Cap Fund"),
    "105503": ("Nippon India Growth Fund - Growth",       "Equity Scheme - Mid Cap Fund"),
    "105989": ("DSP Small Cap Fund - Growth",             "Equity Scheme - Small Cap Fund"),
    "114564": ("Axis Midcap Fund - Growth",               "Equity Scheme - Mid Cap Fund"),
    "100822": ("UTI Nifty 50 Index Fund - Growth",        "Other Scheme - Index Funds"),
    "100177": ("quant Small Cap Fund - Growth",           "Equity Scheme - Small Cap Fund"),
}


def _in_universe(amfi_category: str, internal_category: str) -> bool:
    """Decisions D2 and D3. MODEL_AUDIT.md §14."""
    if internal_category in C.EXCLUDED_INTERNAL_CATEGORIES:
        return False
    broad, sub = get_display_category(amfi_category)
    if sub in C.CORE_SUBS:
        return True
    return bool(C.INCLUDE_BROAD_HYBRID and broad == "Hybrid")


def load_fixture(path: Path | None = None) -> list[Fund]:
    path = path or C.FIXTURE_NAV
    raw = json.loads(path.read_text())
    funds = []
    for code, rows in raw.items():
        name, amfi = FIXTURE_META.get(code, (f"Scheme {code}", ""))
        internal = classify_fund(name, amfi)
        nav = sorted((date.fromisoformat(d), float(v)) for d, v in rows)
        funds.append(Fund(int(code), name, amfi, internal, nav))
    return funds


def load_store(
    store: Path | None = None,
    limit: int | None = None,
    apply_universe_filter: bool = True,
    log=print,
) -> list[Fund]:
    store = store or C.NAV_STORE
    files = sorted(store.glob("*.json"))
    if not files:
        raise FileNotFoundError(
            f"{store} is empty. Run `python -m backtest.download_navs` on the "
            "machine that can reach api.mfapi.in."
        )
    if limit:
        files = files[:limit]

    funds, skipped = [], 0
    for p in files:
        try:
            d = json.loads(p.read_text())
        except Exception:
            skipped += 1
            continue
        internal = d.get("internal_category") or classify_fund(
            d["scheme_name"], d.get("scheme_category", "")
        )
        if apply_universe_filter and not _in_universe(
            d.get("scheme_category", ""), internal
        ):
            skipped += 1
            continue
        nav = sorted((date.fromisoformat(r[0]), float(r[1])) for r in d["nav"])
        if len(nav) < 2:
            skipped += 1
            continue
        funds.append(Fund(
            scheme_code=int(d["scheme_code"]),
            scheme_name=d["scheme_name"],
            amfi_category=d.get("scheme_category", ""),
            internal_category=internal,
            nav=nav,
        ))
    log(f"loaded {len(funds):,} funds from {store.name} ({skipped:,} skipped)")
    return funds


def dataset_span(funds: list[Fund]) -> tuple[date, date]:
    return (min(f.nav[0][0] for f in funds), max(f.nav[-1][0] for f in funds))


def data_quality_report(funds: list[Fund]) -> list[dict]:
    """MODEL_AUDIT.md §26 — inspect before trusting anything."""
    from . import point_in_time as pit

    rows = []
    for f in funds:
        d = pit.describe(f.nav)
        d.update({
            "scheme_code": f.scheme_code,
            "scheme_name": f.scheme_name,
            "internal_category": f.internal_category,
            "amfi_category": f.amfi_category,
        })
        d["long_gaps"] = json.dumps(d["long_gaps"])
        d["splices"] = json.dumps(d["splices"])
        rows.append(d)
    return rows
