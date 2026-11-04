"""Library entry point for building the fund lineage database.

This is the importable half of scripts/build_lineage.py — pulled out so
server.py can trigger a real, full-universe build in-process (POST
/api/admin/build-lineage) against the SAME `db` connection the rest of the
app already uses, instead of only being runnable as a standalone CLI script
against a manually-supplied MONGO_URL. The CLI script is now a thin wrapper
around `build_lineage()` below — behaviour is unchanged, nothing here is
duplicated.

Why this needed to exist as a separate, in-process trigger at all: this app's
production database lives behind credentials the development environment
does not have. `fund_lineage` / `fund_successor_map` can only actually be
populated for the real fund universe by something running WITH those
credentials — i.e. the deployed backend itself. This endpoint is that
something: an authenticated advisor (or the deploy pipeline) triggers it
once the app is live, the same way /api/home/reindex and
/api/admin/merger-discovery already work.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from typing import Any, Dict, Optional

import amfi_history as ah
import lineage_store as ls
import fund_lineage


async def build_lineage(
    db: Any,
    *,
    out_dir: Optional[str] = None,
    quick: bool = False,
    persist: bool = True,
    hist_years: int = 20,
    log=print,
) -> Dict[str, Any]:
    """Derive the lineage database and (by default) persist it.

    `db` is an already-connected motor database — the caller's own
    connection, never a new one opened here. `out_dir`, when given, also
    writes the reviewable CSV/JSON artifacts (same shape as the CLI script);
    omit it for a DB-only run (the server endpoint does this, since there is
    nowhere durable to keep files on Render between requests).
    """
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    sources: Dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "live": ah.LIVE_URL, "archived": [], "historical": [],
    }

    # ── 1. Archived AMFI captures — the only source of period-correct names ──
    stamps = await ah.list_wayback_captures()
    if quick:
        stamps = stamps[:: max(1, len(stamps) // 6)][:6]
    log(f"archived captures to fetch: {len(stamps)}")
    archived = await ah.crawl_wayback(stamps, concurrency=2)
    sources["archived"] = [
        {"timestamp": s, "date": ah.wayback_date(s),
         "url": ah.WAYBACK_SNAPSHOT.format(ts=s),
         "schemes": len(archived.get(ah.wayback_date(s), []))}
        for s in stamps
    ]
    log(f"archived captures parsed: {len(archived)}")

    # ── 2. AMFI historical report — existence/wound-up dating back to ~2006 ──
    hist_windows = [
        w for w in ah.quarterly_windows(start=date(date.today().year - hist_years, 1, 1))
    ]
    if quick:
        hist_windows = hist_windows[::4]
    log(f"historical windows to fetch: {len(hist_windows)}")
    historical = await ah.crawl_snapshots(hist_windows, concurrency=3)
    historical = {k: v for k, v in historical.items() if v}
    sources["historical"] = [
        {"window_start": s.isoformat(), "window_end": e.isoformat(),
         "url": ah.HISTORY_URL, "schemes": len(historical.get(s.isoformat(), []))}
        for s, e in hist_windows
    ]
    log(f"historical windows parsed: {len(historical)}")

    # ── 3. Live AMFI file — current identity, AMC, scheme type ──────────────
    live = await ah.fetch_live()
    log(f"live schemes: {len(live)}")

    # ── 4. Derive ─────────────────────────────────────────────────────────
    records = ls.derive_lineage(archived, live=live, existence_only=historical)
    log(f"records derived: {len(records)}")

    # ── 5. Human-verified successors (the only mappings we assert) ─────────
    curated = list(fund_lineage.CURATED_SUCCESSORS.items())
    applied = ls.apply_successors(records, curated, ls.Source.CURATED)
    log(f"curated successor mappings applied: {applied}")

    # ── 5b. Curated same-code AMC changes — source-verified, applied on top ─
    # Covers the dominant real-world case (AMC rename/acquisition with no
    # scheme-code change) even when the archive-derived pass above found no
    # AMC_CHANGE actions at all (e.g. archive.org unreachable this run —
    # step 1 logs archived captures: 0 when that happens). Idempotent against
    # a future run where archive.org IS reachable: apply_amc_changes() skips
    # any action already present for the same effective_date.
    amc_applied = ls.apply_amc_changes(records, fund_lineage.CURATED_AMC_CHANGES, fund_lineage.canonical_house)
    log(f"curated AMC-change actions applied: {amc_applied}")

    # ── 6. Machine-proposed successors — exported, NOT applied ─────────────
    candidates = ls.derive_merger_candidates(records, fund_lineage.canonical_house)
    log(f"merger candidates proposed (unverified): {len(candidates)}")

    meta = ls.build_metadata(records, sources)
    summary = ls.summarise(records)
    summary.update(meta)
    summary.update({
        "merger_candidates_unverified": len(candidates),
        "curated_mappings_applied": applied,
        "archived_captures_used": len(archived),
        "historical_windows_used": len(historical),
        "name_history_coverage_start": min(archived) if archived else None,
        "existence_coverage_start": min(historical) if historical else None,
    })

    # ── 7. Optional file artifacts (CLI path only) ──────────────────────────
    if out_dir:
        action_rows, fund_rows = ls.export_rows(records)
        n_actions = ls.write_csv(os.path.join(out_dir, "lineage_actions.csv"),
                                 action_rows, ls.ACTION_COLUMNS)
        n_funds = ls.write_csv(os.path.join(out_dir, "lineage_funds.csv"),
                               fund_rows, ls.FUND_COLUMNS)
        if candidates:
            ls.write_csv(os.path.join(out_dir, "merger_candidates.csv"),
                         candidates, list(candidates[0].keys()))
        with open(os.path.join(out_dir, "lineage.json"), "w", encoding="utf-8") as fh:
            json.dump({str(c): r.to_doc() for c, r in records.items()}, fh, indent=1)
        with open(os.path.join(out_dir, "sources.json"), "w", encoding="utf-8") as fh:
            json.dump(sources, fh, indent=1)
        with open(os.path.join(out_dir, "lineage_meta.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2)
        summary["action_rows_exported"] = n_actions
        summary["fund_rows_exported"] = n_funds
        with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2)

    # ── 8. Persist ────────────────────────────────────────────────────────
    if persist:
        counts = await ls.persist(db, records, meta=meta)
        log(f"persisted: {counts}")
        summary["persisted"] = counts

    return summary
