"""Fund lineage database — identity continuity across corporate actions.

What this replaces
──────────────────
`fund_lineage.CURATED_SUCCESSORS` is a hand-maintained dict of three verified
merger pairs. It works, but it does not scale: every new AMC merger needs a
code change and a redeploy, and it can only ever describe cross-code mergers —
never a rename, an ISIN change, or an AMC acquisition that kept the code.

This module builds a real lineage table instead, derived from evidence rather
than hardcoded. It does NOT replace the curated map: curated pairs remain the
highest-trust source and are merged in on top (see `CURATED` provenance).

Two collections
───────────────
  fund_lineage        one document per scheme code — the identity spine, with
                      full name / ISIN / AMC history and the corporate actions
                      derived from the transitions between them.

  fund_identifier_idx flat reverse index, one document per (identifier, code)
                      pair. This is what makes "search by any historical name,
                      any old code, or any ISIN" a single indexed lookup rather
                      than a scan.

Provenance, and why it is on every record
─────────────────────────────────────────
Not all evidence is equal. An AMFI snapshot showing the same scheme code under
two different names IS a rename — observed, not inferred. A correlation-matched
predecessor is a strong inference but still an inference. A human-curated pair
is authoritative but unverifiable at runtime. Mixing these without labelling
them would make the whole table only as trustworthy as its weakest row, so
every corporate action carries `source` and `confidence`, and consumers can
demand a floor.

Coverage limits — stated, not hidden
────────────────────────────────────
AMFI's downloadable history begins around 2006 (Jan-2003 returns an empty
report). Lineage before that date cannot be derived from this source. Records
therefore carry `observed_from`, and a fund whose true inception predates it is
flagged rather than being presented as if 2006 were its inception.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from amfi_history import LINEAGE_COVERAGE_START, SchemeObservation, is_valid_isin

logger = logging.getLogger("lineage_store")

LINEAGE_COLLECTION = "fund_lineage"
IDENTIFIER_COLLECTION = "fund_identifier_idx"
REVIEW_COLLECTION = "fund_lineage_review"
META_COLLECTION = "fund_lineage_meta"
# Promoted successor mappings — the DB-backed replacement for hardcoding
# pairs in fund_lineage.CURATED_SUCCESSORS, so a new AMC merger needs a
# discovery run and a review, not a code change and a redeploy.
SUCCESSOR_COLLECTION = "fund_successor_map"

# Bump on any change to derivation, classification or the action taxonomy.
# Two builds are only comparable when their generator_version matches —
# otherwise a "changed corporate action" in a diff may just be this module
# having learned to classify it differently.
GENERATOR_VERSION = "1.1.0"

# 1.0.0  initial derivation + resolver
# 1.1.0  split back-filled sources out of name history (phantom-rename fix);
#        added NAME_COSMETIC / NAME_TERMINOLOGY classification


def build_metadata(
    records: Dict[int, "LineageRecord"],
    sources: Dict[str, Any],
) -> Dict[str, Any]:
    """Reproducibility header for an export.

    `lineage_version` is a CONTENT hash, not a counter: two builds from the
    same inputs get the same id even if run days apart, and any change to the
    derived data changes it. That makes "did anything actually change?"
    answerable without diffing 39 MB.
    """
    import hashlib
    from datetime import datetime, timezone

    digest = hashlib.sha256()
    for code in sorted(records):
        rec = records[code]
        digest.update(str(code).encode())
        digest.update(rec.current_name.encode("utf-8", "replace"))
        digest.update(rec.current_isin.encode())
        for a in rec.corporate_actions:
            digest.update(f"{a['type']}|{a.get('effective_date')}|"
                          f"{a.get('from')}|{a.get('to')}|{a.get('source')}".encode("utf-8", "replace"))
        for p in sorted(rec.predecessor_codes):
            digest.update(f"pred:{p}".encode())

    src_digest = hashlib.sha256(
        json.dumps(sources, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]

    return {
        "lineage_version": digest.hexdigest()[:16],
        "generator_version": GENERATOR_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_snapshot": src_digest,
        "source_counts": {
            "archived_captures": len(sources.get("archived", [])),
            "historical_windows": len(sources.get("historical", [])),
            "live": bool(sources.get("live")),
        },
        "record_count": len(records),
        "coverage_start": LINEAGE_COVERAGE_START.isoformat(),
    }


def diff_lineage(old_doc: Dict[str, Any], new_doc: Dict[str, Any]) -> Dict[str, Any]:
    """Compare two exported lineage.json payloads.

    Reports what a reviewer needs to sign off an update: which funds appeared
    or vanished, which successor mappings were added or withdrawn, which
    corporate actions changed, and which confidence scores moved.
    """
    old_codes, new_codes = set(old_doc), set(new_doc)

    def actions(doc, code):
        return {
            (a["type"], a.get("effective_date"), a.get("from"), a.get("to"))
            for a in doc[code].get("corporate_actions", [])
        }

    added_map, removed_map, changed_conf, changed_actions = [], [], [], []
    for code in sorted(old_codes & new_codes):
        o, n = old_doc[code], new_doc[code]

        o_pred, n_pred = set(o.get("predecessor_codes") or []), set(n.get("predecessor_codes") or [])
        for p in sorted(n_pred - o_pred):
            added_map.append({"scheme_code": code, "predecessor_code": p})
        for p in sorted(o_pred - n_pred):
            removed_map.append({"scheme_code": code, "predecessor_code": p})
        if o.get("successor_code") != n.get("successor_code"):
            (added_map if n.get("successor_code") else removed_map).append(
                {"scheme_code": code, "successor_code": n.get("successor_code"),
                 "was": o.get("successor_code")})

        oa, na = actions(old_doc, code), actions(new_doc, code)
        if oa != na:
            changed_actions.append({
                "scheme_code": code,
                "added": sorted(f"{t}@{d}" for t, d, _, _ in (na - oa)),
                "removed": sorted(f"{t}@{d}" for t, d, _, _ in (oa - na)),
            })

        o_conf = {(a["type"], a.get("effective_date")): a.get("confidence")
                  for a in o.get("corporate_actions", [])}
        for a in n.get("corporate_actions", []):
            k = (a["type"], a.get("effective_date"))
            if k in o_conf and o_conf[k] != a.get("confidence"):
                changed_conf.append({"scheme_code": code, "action": k[0],
                                     "from": o_conf[k], "to": a.get("confidence")})

    return {
        "funds_added": sorted(new_codes - old_codes),
        "funds_removed": sorted(old_codes - new_codes),
        "mappings_added": added_map,
        "mappings_removed": removed_map,
        "actions_changed": changed_actions,
        "confidence_changed": changed_conf,
        "summary": {
            "funds_added": len(new_codes - old_codes),
            "funds_removed": len(old_codes - new_codes),
            "mappings_added": len(added_map),
            "mappings_removed": len(removed_map),
            "funds_with_action_changes": len(changed_actions),
            "confidence_changes": len(changed_conf),
        },
    }


# ── Corporate action taxonomy ────────────────────────────────────────────────

class Action:
    NAME_CHANGE = "NAME_CHANGE"                 # substantive rename
    NAME_COSMETIC = "NAME_COSMETIC"             # case / punctuation / spacing only
    NAME_TERMINOLOGY = "NAME_TERMINOLOGY"       # SEBI Dividend→IDCW relabel etc.
    AMC_CHANGE = "AMC_CHANGE"          # fund-house rename or AMC acquisition
    ISIN_CHANGE = "ISIN_CHANGE"
    RECATEGORISATION = "RECATEGORISATION"   # SEBI 2018 scheme-category reshuffle
    MERGER = "MERGER"                  # wound up, unit-holders moved to another code
    WOUND_UP = "WOUND_UP"              # stopped publishing, no successor identified


class Source:
    AMFI_SNAPSHOT = "AMFI_SNAPSHOT"    # observed directly in AMFI files
    CURATED = "CURATED"                # human-verified pair
    CORRELATION = "CORRELATION"        # NAV return-correlation match


# Confidence floors by provenance. AMFI_SNAPSHOT is 1.0 because it is an
# observation of AMFI's own record, not an inference about it.
SOURCE_CONFIDENCE = {
    Source.AMFI_SNAPSHOT: 1.0,
    Source.CURATED: 0.98,
    Source.CORRELATION: 0.90,
}

# SEBI's recategorisation circular window. A name change inside this window
# that also changes the scheme's category words is a recategorisation, not a
# vanity rename — worth distinguishing because it explains a strategy change.
SEBI_RECAT_START = "2017-10-01"
SEBI_RECAT_END = "2019-06-30"

_CATEGORY_WORDS = (
    "large cap", "mid cap", "small cap", "multi cap", "flexi cap",
    "large & mid", "large and mid", "focused", "value", "contra",
    "dividend yield", "elss", "index", "arbitrage", "balanced",
    "aggressive hybrid", "conservative hybrid", "equity savings",
    "liquid", "overnight", "ultra short", "low duration", "money market",
    "short duration", "medium duration", "long duration", "dynamic bond",
    "corporate bond", "credit risk", "banking and psu", "gilt", "floater",
)


# ── Name normalisation ───────────────────────────────────────────────────────
# Search must match on meaning, not punctuation. AMFI writes the same scheme as
# "Aditya Birla Sun Life Banking & PSU Debt Fund  - DIRECT - IDCW" in one file
# and "Aditya Birla Sun Life Banking and PSU Debt Fund-Direct-IDCW" in another.

_PLAN_NOISE = re.compile(
    r"\b(regular|direct|growth|idcw|dividend|payout|reinvestment|re-investment|"
    r"plan|option|scheme|fund)\b",
    re.IGNORECASE,
)
_PUNCT = re.compile(r"[^a-z0-9&\s]")
_WS = re.compile(r"\s+")


def normalise_name(name: str) -> str:
    """Canonical form for matching: lowercase, '&'→'and', punctuation stripped.

    Keeps plan/option words — 'HDFC Top 100 Growth' and 'HDFC Top 100 IDCW' are
    genuinely different schemes with different NAVs, so collapsing them would
    merge two funds. Use `name_stem()` when you deliberately want them together.
    """
    s = (name or "").lower().replace("&", " and ")
    s = _PUNCT.sub(" ", s)
    return _WS.sub(" ", s).strip()


def name_stem(name: str) -> str:
    """Normalised name with plan/option words removed — the 'same underlying
    scheme' key. Used for fuzzy search recall, never for identity."""
    s = normalise_name(name)
    s = _PLAN_NOISE.sub(" ", s)
    return _WS.sub(" ", s).strip()


def _is_recat(old: str, new: str, when: str) -> bool:
    """A rename during the SEBI window that changes the category words."""
    if not (SEBI_RECAT_START <= when <= SEBI_RECAT_END):
        return False
    o, n = normalise_name(old), normalise_name(new)
    o_cats = {c for c in _CATEGORY_WORDS if c in o}
    n_cats = {c for c in _CATEGORY_WORDS if c in n}
    return o_cats != n_cats


# SEBI's Oct-2021 circular replaced "Dividend" with "IDCW" (Income Distribution
# cum Capital Withdrawal) in every scheme name. AMCs applied it on their own
# schedules, so it shows up as thousands of "renames" that changed no fund.
_PAYOUT_SYNONYMS = re.compile(
    r"\b(idcw|dividend|income distribution cum capital withdrawal)\b", re.IGNORECASE)
_REINVEST_SYNONYMS = re.compile(
    r"\b(reinvestment|re-investment|reinvest)\b", re.IGNORECASE)


def classify_name_change(old: str, new: str, when: str) -> str:
    """Decide what a name transition actually represents.

    Without this, the headline "name changes" figure is dominated by noise.
    Verified against 5 randomly sampled records re-fetched from their source
    captures: 4 of 5 were case changes, punctuation changes, a Dividend→IDCW
    relabel, or an FMP maturity-date text correction — only 1 was a real
    corporate action. Reporting all of them as renames would materially
    overstate how much lineage work the data actually represents.

    Order matters: cosmetic is checked first because a pure case change can
    also look like a terminology change once lowercased.
    """
    if normalise_name(old) == normalise_name(new):
        return Action.NAME_COSMETIC

    def payout_neutral(s: str) -> str:
        s = normalise_name(s)
        s = _PAYOUT_SYNONYMS.sub("payout", s)
        s = _REINVEST_SYNONYMS.sub("reinvest", s)
        return _WS.sub(" ", s).strip()

    if payout_neutral(old) == payout_neutral(new):
        return Action.NAME_TERMINOLOGY

    if _is_recat(old, new, when):
        return Action.RECATEGORISATION

    return Action.NAME_CHANGE


# ── Derivation (pure — no DB, no network, fully unit-testable) ───────────────

@dataclass
class Interval:
    """A value (name / ISIN / AMC) and the span over which it was observed."""
    value: str
    first_seen: str
    last_seen: str

    def to_dict(self) -> Dict[str, Any]:
        return {"value": self.value, "first_seen": self.first_seen, "last_seen": self.last_seen}


@dataclass
class LineageRecord:
    scheme_code: int
    current_name: str = ""
    current_isin: str = ""
    current_amc: str = ""
    scheme_type: str = ""
    name_history: List[Interval] = field(default_factory=list)
    isin_history: List[Interval] = field(default_factory=list)
    amc_history: List[Interval] = field(default_factory=list)
    corporate_actions: List[Dict[str, Any]] = field(default_factory=list)
    observed_from: str = ""
    observed_to: str = ""
    active: bool = False
    successor_code: Optional[int] = None
    predecessor_codes: List[int] = field(default_factory=list)
    review: List[Dict[str, str]] = field(default_factory=list)

    def to_doc(self) -> Dict[str, Any]:
        return {
            "scheme_code": self.scheme_code,
            "current_name": self.current_name,
            "current_isin": self.current_isin,
            "current_amc": self.current_amc,
            "scheme_type": self.scheme_type,
            "name_history": [i.to_dict() for i in self.name_history],
            "isin_history": [i.to_dict() for i in self.isin_history],
            "amc_history": [i.to_dict() for i in self.amc_history],
            "corporate_actions": self.corporate_actions,
            "observed_from": self.observed_from,
            "observed_to": self.observed_to,
            "active": self.active,
            "successor_code": self.successor_code,
            "predecessor_codes": self.predecessor_codes,
            "review": self.review,
            "coverage_start": LINEAGE_COVERAGE_START.isoformat(),
        }


def _append_interval(hist: List[Interval], value: str, when: str) -> bool:
    """Extend the run if `value` is unchanged, else open a new one.

    Returns True when a NEW distinct value began — i.e. a change occurred.
    """
    if not value:
        return False
    if hist and hist[-1].value == value:
        if when > hist[-1].last_seen:
            hist[-1].last_seen = when
        return False
    hist.append(Interval(value=value, first_seen=when, last_seen=when))
    return len(hist) > 1


def derive_lineage(
    snapshots: Dict[str, Sequence[SchemeObservation]],
    live: Optional[Sequence[SchemeObservation]] = None,
    existence_only: Optional[Dict[str, Sequence[SchemeObservation]]] = None,
) -> Dict[int, LineageRecord]:
    """Build per-code lineage from dated snapshots.

    `snapshots` — sources whose NAMES ARE TRUSTWORTHY AT THEIR DATE (archived
    AMFI captures). Processing them chronologically turns a series of
    point-in-time observations into a name history, and each transition into a
    dated corporate action.

    `existence_only` — sources that report WHICH CODES EXISTED but whose names
    are back-filled with today's value (AMFI's historical NAV report). These
    must never touch name/ISIN/AMC history.

    Why the split is mandatory, not stylistic
    ─────────────────────────────────────────
    Feeding both through one path produces impossible histories. A 2014 row
    from the back-filled source carries the 2026 name; a 2016 archived capture
    carries the real 2016 name. Sorted by date they alternate, so scheme 118778
    came out as Nippon(2014) → Reliance(2016) → Nippon(2017) → Reliance(2017) →
    Nippon(2018): five intervals and four "renames" for a fund that was renamed
    once. Measured across a real run this inflated NAME_CHANGE from 13,644 to
    35,109 and left 7,747 funds whose history revisits a name they had already
    left. Existence data is still valuable — it reaches back to ~2006, far
    earlier than any archive capture — but only for dating first/last sighting.

    `live` (today's NAVAll.txt) supplies AMC and scheme type, which neither
    historical source carries, and marks a code as active.
    """
    records: Dict[int, LineageRecord] = {}

    # Existence pass first: seed observed_from/observed_to only. No name, ISIN
    # or AMC is read from these rows.
    for when in sorted(existence_only or {}):
        for obs in (existence_only or {})[when]:
            rec = records.get(obs.scheme_code)
            if rec is None:
                rec = LineageRecord(scheme_code=obs.scheme_code, observed_from=when)
                records[obs.scheme_code] = rec
            if when < rec.observed_from or not rec.observed_from:
                rec.observed_from = when
            if when > rec.observed_to:
                rec.observed_to = when

    for when in sorted(snapshots):
        for obs in snapshots[when]:
            rec = records.get(obs.scheme_code)
            if rec is None:
                rec = LineageRecord(scheme_code=obs.scheme_code, observed_from=when)
                records[obs.scheme_code] = rec
            if not rec.observed_from or when < rec.observed_from:
                rec.observed_from = when
            if when > rec.observed_to:
                rec.observed_to = when

            prev_name = rec.name_history[-1].value if rec.name_history else None
            if _append_interval(rec.name_history, obs.scheme_name, when) and prev_name:
                kind = classify_name_change(prev_name, obs.scheme_name, when)
                rec.corporate_actions.append({
                    "type": kind,
                    "effective_date": when,
                    "from": prev_name,
                    "to": obs.scheme_name,
                    "source": Source.AMFI_SNAPSHOT,
                    "confidence": SOURCE_CONFIDENCE[Source.AMFI_SNAPSHOT],
                })

            if obs.isin_growth:
                if not is_valid_isin(obs.isin_growth):
                    rec.review.append({
                        "reason": "MALFORMED_ISIN",
                        "detail": f"{obs.isin_growth!r} observed {when}; not indexed",
                    })
                else:
                    prev_isin = rec.isin_history[-1].value if rec.isin_history else None
                    if _append_interval(rec.isin_history, obs.isin_growth, when) and prev_isin:
                        rec.corporate_actions.append({
                            "type": Action.ISIN_CHANGE,
                            "effective_date": when,
                            "from": prev_isin,
                            "to": obs.isin_growth,
                            "source": Source.AMFI_SNAPSHOT,
                            "confidence": SOURCE_CONFIDENCE[Source.AMFI_SNAPSHOT],
                        })

            if obs.amc_name:
                prev_amc = rec.amc_history[-1].value if rec.amc_history else None
                if _append_interval(rec.amc_history, obs.amc_name, when) and prev_amc:
                    rec.corporate_actions.append({
                        "type": Action.AMC_CHANGE,
                        "effective_date": when,
                        "from": prev_amc,
                        "to": obs.amc_name,
                        "source": Source.AMFI_SNAPSHOT,
                        "confidence": SOURCE_CONFIDENCE[Source.AMFI_SNAPSHOT],
                    })

    # Live pass — AMC / scheme type / active flag, and current values.
    live_codes: Set[int] = set()
    for obs in (live or []):
        live_codes.add(obs.scheme_code)
        rec = records.get(obs.scheme_code)
        if rec is None:
            rec = LineageRecord(scheme_code=obs.scheme_code, observed_from="live")
            records[obs.scheme_code] = rec
        rec.active = True
        rec.current_name = obs.scheme_name or rec.current_name
        rec.current_amc = obs.amc_name
        rec.scheme_type = obs.scheme_type
        if obs.isin_growth and is_valid_isin(obs.isin_growth):
            rec.current_isin = obs.isin_growth
        if obs.amc_name:
            prev_amc = rec.amc_history[-1].value if rec.amc_history else None
            if _append_interval(rec.amc_history, obs.amc_name, "live") and prev_amc:
                rec.corporate_actions.append({
                    "type": Action.AMC_CHANGE,
                    "effective_date": "live",
                    "from": prev_amc,
                    "to": obs.amc_name,
                    "source": Source.AMFI_SNAPSHOT,
                    "confidence": SOURCE_CONFIDENCE[Source.AMFI_SNAPSHOT],
                })

    # Fill current_* from the last observation for codes absent from the live
    # file, and mark them wound up — they stopped publishing NAVs.
    for code, rec in records.items():
        if not rec.current_name and rec.name_history:
            rec.current_name = rec.name_history[-1].value
        if not rec.current_isin and rec.isin_history:
            rec.current_isin = rec.isin_history[-1].value
        if not rec.current_amc and rec.amc_history:
            rec.current_amc = rec.amc_history[-1].value
        if live and code not in live_codes:
            rec.active = False
            rec.corporate_actions.append({
                "type": Action.WOUND_UP,
                "effective_date": rec.observed_to,
                "from": rec.current_name,
                "to": None,
                "source": Source.AMFI_SNAPSHOT,
                "confidence": SOURCE_CONFIDENCE[Source.AMFI_SNAPSHOT],
            })
    return records


def apply_successors(
    records: Dict[int, LineageRecord],
    pairs: Iterable[Tuple[int, int]],
    source: str,
    confidence: Optional[float] = None,
) -> int:
    """Attach predecessor→successor links (mergers) onto derived records.

    `pairs` is (wound_up_code, successor_code). Both directions are written so
    resolution works from either end. Returns the number of links applied.
    """
    conf = confidence if confidence is not None else SOURCE_CONFIDENCE.get(source, 0.9)
    applied = 0
    for pred, succ in pairs:
        pred, succ = int(pred), int(succ)
        if pred == succ:
            continue
        rec = records.get(pred)
        if rec is None:
            rec = LineageRecord(scheme_code=pred)
            records[pred] = rec
        rec.successor_code = succ
        rec.corporate_actions.append({
            "type": Action.MERGER,
            "effective_date": rec.observed_to or "",
            "from": rec.current_name,
            "to": records[succ].current_name if succ in records else str(succ),
            "successor_code": succ,
            "source": source,
            "confidence": conf,
        })
        srec = records.get(succ)
        if srec is None:
            srec = LineageRecord(scheme_code=succ)
            records[succ] = srec
        if pred not in srec.predecessor_codes:
            srec.predecessor_codes.append(pred)
        applied += 1
    return applied


def apply_amc_changes(
    records: Dict[int, LineageRecord],
    events: Iterable[Dict[str, Any]],
    canonical_house_fn,
) -> int:
    """Attach curated SAME-CODE AMC-change actions on top of derived records.

    `events` is fund_lineage.CURATED_AMC_CHANGES — house-level facts, each
    with `from_house`/`to_house`/`effective_date`. Applied generically: every
    record whose CURRENT amc canonicalises to an event's `to_house`, and
    which already existed before `effective_date`, gets the action — so one
    curated entry covers an AMC's entire fund range automatically, the same
    "fix it once per house, not once per fund" shape as apply_successors()
    for cross-code mergers.

    A record that only started existing after the transition never went
    through it and is correctly skipped (existed_from is compared, not
    ignored). Idempotent: re-running with the same events does not duplicate
    an action already present (from a prior run of this function OR from the
    archive-derived pass finding the same transition independently).
    """
    by_to_house: Dict[str, List[Dict[str, Any]]] = {}
    for ev in events:
        by_to_house.setdefault(canonical_house_fn(ev["to_house"]), []).append(ev)

    conf = SOURCE_CONFIDENCE[Source.CURATED]
    applied = 0
    for rec in records.values():
        if not rec.current_amc:
            continue
        candidates = by_to_house.get(canonical_house_fn(rec.current_amc))
        if not candidates:
            continue
        existed_from = rec.observed_from or ""
        for ev in candidates:
            eff = ev["effective_date"]
            # Require POSITIVE evidence the fund existed before the
            # transition. "live"/"" (no existence data reached this record —
            # sparse historical coverage, or a genuinely new fund) fails
            # closed here: asserting a management change this fund may never
            # have gone through is a worse error than missing one that a
            # fuller existence crawl would later confirm.
            if not existed_from or existed_from == "live" or existed_from > eff:
                continue
            already = any(
                a.get("type") == Action.AMC_CHANGE and a.get("effective_date") == eff
                for a in rec.corporate_actions
            )
            if already:
                continue
            rec.corporate_actions.append({
                "type": Action.AMC_CHANGE,
                "effective_date": eff,
                "from": ev["from_house"],
                "to": ev["to_house"],
                "source": Source.CURATED,
                "confidence": conf,
                "sources": ev.get("sources", []),
            })
            applied += 1

            # Splice amc_history when it is still just the single live-only
            # placeholder interval derive_lineage() creates in the absence of
            # archived captures (see that function's live pass) — this is
            # what makes identity_history.amcs show the real "then vs now"
            # chain instead of only today's name, without needing archive.org
            # to have been reachable. Left untouched when amc_history already
            # has real dated intervals (from an archive-derived run), so a
            # curated overlay can never corrupt richer derived data.
            if (len(rec.amc_history) == 1
                    and rec.amc_history[0].first_seen == "live"
                    and rec.amc_history[0].last_seen == "live"):
                rec.amc_history[0] = Interval(
                    value=ev["to_house"], first_seen=eff, last_seen="live")
                rec.amc_history.insert(0, Interval(
                    value=ev["from_house"],
                    first_seen=rec.observed_from or eff, last_seen=eff))

        rec.corporate_actions.sort(key=lambda a: a.get("effective_date") or "")
    return applied


# ── Identifier index ─────────────────────────────────────────────────────────

def build_identifier_index(records: Dict[int, LineageRecord]) -> List[Dict[str, Any]]:
    """Flatten every identifier a fund has EVER had into lookup rows.

    One row per (key, kind, scheme_code). A historical name shared by two codes
    (it happens — a merged scheme's name gets reused) yields two rows, and the
    resolver returns both rather than silently picking one.
    """
    rows: List[Dict[str, Any]] = []
    seen: Set[Tuple[str, str, int]] = set()

    def add(key: str, kind: str, code: int, current: bool) -> None:
        key = key.strip()
        if not key:
            return
        sig = (key, kind, code)
        if sig in seen:
            return
        seen.add(sig)
        rows.append({"key": key, "kind": kind, "scheme_code": code, "is_current": current})

    for code, rec in records.items():
        add(str(code), "code", code, True)
        for i, iv in enumerate(rec.name_history):
            is_cur = (iv.value == rec.current_name)
            add(normalise_name(iv.value), "name", code, is_cur)
            add(name_stem(iv.value), "name_stem", code, is_cur)
        if rec.current_name:
            add(normalise_name(rec.current_name), "name", code, True)
            add(name_stem(rec.current_name), "name_stem", code, True)
        for iv in rec.isin_history:
            add(iv.value.upper(), "isin", code, iv.value == rec.current_isin)
        if rec.current_isin:
            add(rec.current_isin.upper(), "isin", code, True)
        # A predecessor's code must resolve to the surviving fund too.
        for pred in rec.predecessor_codes:
            add(str(pred), "code", code, False)
    return rows


def resolve_local(
    index_rows: Iterable[Dict[str, Any]],
    query: str,
) -> List[int]:
    """In-memory resolver — the same matching the DB-backed one performs.

    Order of attempts, most specific first:
      1. exact scheme code
      2. exact ISIN
      3. exact normalised name (current OR historical)
      4. name stem (plan/option words dropped)
      5. substring on the stem
    """
    q = (query or "").strip()
    if not q:
        return []
    by_kind: Dict[str, Dict[str, List[int]]] = defaultdict(lambda: defaultdict(list))
    for r in index_rows:
        by_kind[r["kind"]][r["key"]].append(r["scheme_code"])

    if q.isdigit() and q in by_kind["code"]:
        return sorted(set(by_kind["code"][q]))
    up = q.upper()
    if up in by_kind["isin"]:
        return sorted(set(by_kind["isin"][up]))
    n = normalise_name(q)
    if n in by_kind["name"]:
        return sorted(set(by_kind["name"][n]))
    stem = name_stem(q)
    if stem and stem in by_kind["name_stem"]:
        return sorted(set(by_kind["name_stem"][stem]))
    if stem:
        hits: Set[int] = set()
        for key, codes in by_kind["name_stem"].items():
            if stem in key:
                hits.update(codes)
        return sorted(hits)
    return []


# ── Persistence ──────────────────────────────────────────────────────────────

BULK_BATCH = 5000
_STAGING_SUFFIX = "__staging"


async def _bulk_load(db, name: str, docs: List[Dict[str, Any]],
                     indexes: List[Any], batch: int = BULK_BATCH) -> None:
    """Load `docs` into a staging collection, index it, then swap it into place.

    Why not upsert in place
    ───────────────────────
    The previous implementation issued one ReplaceOne/UpdateOne per document
    against the LIVE collection. Each op is a separate query + write, and the
    collection is indexed while it is being written, so every insert also
    updates every index. Measured on this dataset: ~2,000 docs/min, i.e. the
    37,714 lineage docs plus 133,499 index rows would take well over an hour.

    Bulk loading instead:
      • insert_many into an EMPTY, UNINDEXED staging collection — no per-doc
        query, no index maintenance during the load,
      • build indexes once, on the complete data,
      • rename staging over the live name atomically (dropTarget=True).

    The swap also makes the operation safe: readers see the previous complete
    dataset right up until the rename, then the new complete one. The old
    in-place upsert left the collection half-old/half-new for the entire load,
    so a search during a rebuild could resolve against a mixture of two builds.
    """
    staging = f"{name}{_STAGING_SUFFIX}"
    await db[staging].drop()
    if docs:
        for i in range(0, len(docs), batch):
            await db[staging].insert_many(docs[i:i + batch], ordered=False)
    for spec in indexes:
        if isinstance(spec, tuple):
            keys, kwargs = spec
            await db[staging].create_index(keys, **kwargs)
        else:
            await db[staging].create_index(spec)
    # rename is atomic; dropTarget replaces the live collection in one step
    await db.client.admin.command(
        "renameCollection",
        f"{db.name}.{staging}",
        to=f"{db.name}.{name}",
        dropTarget=True,
    )


async def persist(db, records: Dict[int, LineageRecord],
                  meta: Optional[Dict[str, Any]] = None) -> Dict[str, int]:
    """Write lineage + identifier index + review queue via staged bulk load.

    `meta` (from build_metadata) is stamped on every lineage document and
    stored in META_COLLECTION, so any row can be traced back to the build that
    produced it. META_COLLECTION is upserted rather than swapped — it is the
    version ledger and must retain prior builds for diffing.
    """
    stamp = {"lineage_version": (meta or {}).get("lineage_version"),
             "generator_version": (meta or {}).get("generator_version"),
             "generated_at": (meta or {}).get("generated_at")}

    lineage_docs = [{**r.to_doc(), **stamp} for r in records.values()]
    await _bulk_load(db, LINEAGE_COLLECTION, lineage_docs,
                     [([("scheme_code", 1)], {"unique": True})])

    index_rows = build_identifier_index(records)
    await _bulk_load(db, IDENTIFIER_COLLECTION,
                     [{**r, **stamp} for r in index_rows],
                     [([("kind", 1), ("key", 1)], {}),
                      ([("scheme_code", 1)], {})])

    review = [
        {"scheme_code": c, "current_name": r.current_name, "items": r.review}
        for c, r in records.items() if r.review
    ]
    await _bulk_load(db, REVIEW_COLLECTION, review,
                     [([("scheme_code", 1)], {"unique": True})])

    if meta:
        # "current" always points at the newest build; each build is also kept
        # under its own content hash so a diff can run against any prior
        # version without re-crawling the sources.
        await db[META_COLLECTION].replace_one(
            {"_id": "current"}, {"_id": "current", **meta}, upsert=True)
        await db[META_COLLECTION].replace_one(
            {"_id": meta["lineage_version"]},
            {"_id": meta["lineage_version"], **meta}, upsert=True)

    return {
        "lineage_records": len(records),
        "identifier_rows": len(index_rows),
        "review_records": len(review),
    }


async def resolve(db, query: str, limit: int = 25) -> List[int]:
    """DB-backed identifier resolution — mirrors resolve_local's precedence."""
    q = (query or "").strip()
    if not q:
        return []
    coll = db[IDENTIFIER_COLLECTION]

    async def lookup(kind: str, key: str) -> List[int]:
        cur = coll.find({"kind": kind, "key": key}, {"_id": 0, "scheme_code": 1}).limit(limit)
        return [d["scheme_code"] async for d in cur]

    if q.isdigit():
        hit = await lookup("code", q)
        if hit:
            return sorted(set(hit))
    hit = await lookup("isin", q.upper())
    if hit:
        return sorted(set(hit))
    hit = await lookup("name", normalise_name(q))
    if hit:
        return sorted(set(hit))
    stem = name_stem(q)
    if stem:
        hit = await lookup("name_stem", stem)
        if hit:
            return sorted(set(hit))
        cur = coll.find(
            {"kind": "name_stem", "key": {"$regex": re.escape(stem)}},
            {"_id": 0, "scheme_code": 1},
        ).limit(limit)
        return sorted({d["scheme_code"] async for d in cur})
    return []


# ── Merger candidate derivation ──────────────────────────────────────────────

def derive_merger_candidates(
    records: Dict[int, LineageRecord],
    canonical_house,
    max_candidates_per_code: int = 1,
) -> List[Dict[str, Any]]:
    """Propose wound-up → active successor pairs from name + house evidence.

    These are CANDIDATES, not mappings. They are exported for review and are
    deliberately NOT fed to `apply_successors`, because name-stem equality
    alone has a real false-positive rate: two schemes from one house can share
    a stem ("HDFC Top 100" growth vs a closed FMP series with a similar name)
    without one succeeding the other. Promoting a wrong pair silently splices
    one fund's NAV onto another and corrupts every downstream return.

    The existing `predecessor_discovery.verify_stitch` NAV return-correlation
    gate is what turns a candidate into a mapping; that requires fetching both
    NAV series and is run separately.
    """
    active_by_stem: Dict[Tuple[str, str], List[int]] = defaultdict(list)
    for code, rec in records.items():
        if rec.active and rec.current_name:
            house = canonical_house(rec.current_amc or rec.current_name)
            active_by_stem[(house, name_stem(rec.current_name))].append(code)

    out: List[Dict[str, Any]] = []
    for code, rec in records.items():
        if rec.active or rec.successor_code or not rec.current_name:
            continue
        house = canonical_house(rec.current_amc or rec.current_name)
        stem = name_stem(rec.current_name)
        hits = [c for c in active_by_stem.get((house, stem), []) if c != code]
        if not hits or len(hits) > max_candidates_per_code:
            continue  # ambiguous fan-out is worse than no suggestion
        out.append({
            "predecessor_code": code,
            "predecessor_name": rec.current_name,
            "successor_code": hits[0],
            "successor_name": records[hits[0]].current_name,
            "canonical_house": house,
            "matched_stem": stem,
            "evidence": "name_stem+house",
            "confidence": 0.55,          # a lead, not a finding
            "status": "NEEDS_NAV_CORRELATION",
        })
    return out


# ── Reviewable export ────────────────────────────────────────────────────────

# Column order mirrors the lineage-record spec: current identity, then every
# previous identity, then the action that links them, then provenance.
ACTION_COLUMNS = [
    "current_scheme", "previous_scheme_names",
    "current_scheme_code", "previous_scheme_codes",
    "current_isin", "previous_isins",
    "amc", "previous_amc_names",
    "corporate_action_type", "effective_date",
    "source", "confidence_score", "discovery_method",
    "observed_from", "observed_to", "active", "review_flags",
]

FUND_COLUMNS = [
    "scheme_code", "current_scheme", "current_isin", "amc", "scheme_type",
    "active", "observed_from", "observed_to",
    "previous_scheme_names", "previous_scheme_codes", "previous_isins",
    "previous_amc_names", "action_count", "action_types",
    "successor_code", "review_flags",
]

# Which provenance counts as human-verified vs machine-derived.
MANUAL_SOURCES = {Source.CURATED}


def _discovery_method(source: str) -> str:
    return "MANUALLY_VERIFIED" if source in MANUAL_SOURCES else "AUTO_DISCOVERED"


def _prev(hist: List[Interval], current: str) -> str:
    """Every historical value except the one still in force, oldest first."""
    return " | ".join(i.value for i in hist if i.value and i.value != current)


def export_rows(records: Dict[int, LineageRecord]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Return (action_rows, fund_rows) ready for CSV/JSON.

    action_rows: one row per corporate action — the reviewable unit, since a
                 reviewer checks "did this rename actually happen on this date".
    fund_rows:   one row per scheme code — the identity spine, for spot-checking
                 a fund end to end.
    """
    action_rows: List[Dict[str, Any]] = []
    fund_rows: List[Dict[str, Any]] = []

    for code in sorted(records):
        rec = records[code]
        prev_names = _prev(rec.name_history, rec.current_name)
        prev_isins = _prev(rec.isin_history, rec.current_isin)
        prev_amcs = _prev(rec.amc_history, rec.current_amc)
        prev_codes = " | ".join(str(c) for c in rec.predecessor_codes)
        flags = " | ".join(r["reason"] for r in rec.review)

        fund_rows.append({
            "scheme_code": code,
            "current_scheme": rec.current_name,
            "current_isin": rec.current_isin,
            "amc": rec.current_amc,
            "scheme_type": rec.scheme_type,
            "active": rec.active,
            "observed_from": rec.observed_from,
            "observed_to": rec.observed_to,
            "previous_scheme_names": prev_names,
            "previous_scheme_codes": prev_codes,
            "previous_isins": prev_isins,
            "previous_amc_names": prev_amcs,
            "action_count": len(rec.corporate_actions),
            "action_types": " | ".join(sorted({a["type"] for a in rec.corporate_actions})),
            "successor_code": rec.successor_code if rec.successor_code is not None else "",
            "review_flags": flags,
        })

        for a in rec.corporate_actions:
            action_rows.append({
                "current_scheme": rec.current_name,
                "previous_scheme_names": prev_names,
                "current_scheme_code": code,
                "previous_scheme_codes": prev_codes,
                "current_isin": rec.current_isin,
                "previous_isins": prev_isins,
                "amc": rec.current_amc,
                "previous_amc_names": prev_amcs,
                "corporate_action_type": a["type"],
                "effective_date": a.get("effective_date", ""),
                "source": a.get("source", ""),
                "confidence_score": a.get("confidence", ""),
                "discovery_method": _discovery_method(a.get("source", "")),
                "observed_from": rec.observed_from,
                "observed_to": rec.observed_to,
                "active": rec.active,
                "review_flags": flags,
                # Per-action detail — what actually changed.
                "action_from": a.get("from") or "",
                "action_to": a.get("to") or "",
            })
    return action_rows, fund_rows


def write_csv(path, rows: List[Dict[str, Any]], columns: List[str]) -> int:
    import csv
    extra = [c for c in (rows[0].keys() if rows else []) if c not in columns]
    header = columns + extra
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return len(rows)


# ── Explainability ───────────────────────────────────────────────────────────
# Answers "why did this old scheme resolve to this new one?" without anyone
# having to re-derive the lineage by hand. Powers admin/search debugging, audit
# logs and the manual-review queue.

# Weighted evidence model. Weights are a policy decision, kept in one place so
# a reviewer can see and change them rather than reverse-engineering a score.
EVIDENCE_WEIGHTS: Dict[str, float] = {
    "isin_continuity":   0.35,   # same security under two codes — strongest
    "nav_correlation":   0.25,   # the two series track the same portfolio
    "official_source":   0.20,   # AMFI's own "(Formerly Known as …)" record
    "amc_continuity":    0.10,   # same AMC, or a known acquisition alias
    "category_continuity": 0.05,  # same SEBI scheme category
    "lifecycle":         0.05,   # handover shape: predecessor ends, successor runs on
}

# Signals deliberately NOT scored, because no feed available here carries them.
# Listing them keeps the gap visible instead of implying full coverage.
UNAVAILABLE_SIGNALS = {
    "benchmark_continuity": "not present in AMFI NAVAll.txt or mfapi.in",
    "risk_category_continuity": "riskometer is not published in either feed",
    "aum_continuity": "AUM is not published in either feed",
}


def evidence_score(signals: Dict[str, bool]) -> float:
    """Weighted confidence from independent evidence. Missing signal == absent."""
    return round(sum(w for k, w in EVIDENCE_WEIGHTS.items() if signals.get(k)), 4)


_MATCH_REASON = {
    "code": "Matched the current AMFI scheme code",
    "isin": "Matched an ISIN on record for this fund",
    "name": "Matched a scheme name on record for this fund",
    "name_stem": "Matched the scheme name ignoring plan/option wording",
}


def explain_record(
    query: str,
    rec: Dict[str, Any],
    matched_kind: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the explanation object for one resolved fund.

    `rec` is a persisted fund_lineage document. Everything here is derived from
    what is actually stored — no reason is asserted that the record does not
    support.
    """
    q = (query or "").strip()
    qn, qs, qu = normalise_name(q), name_stem(q), q.upper()

    reasons: List[str] = []
    sources: List[str] = []
    signals: Dict[str, bool] = {}

    # Which identifier did the query actually hit?
    hist_names = [i["value"] for i in rec.get("name_history", [])]
    hist_isins = [i["value"] for i in rec.get("isin_history", [])]
    current_name = rec.get("current_name", "")

    if q.isdigit() and int(q) != rec.get("scheme_code"):
        reasons.append(
            f"Scheme code {q} is a predecessor of {rec.get('scheme_code')} "
            f"({current_name})")
        signals["isin_continuity"] = True
    elif q.isdigit():
        reasons.append(_MATCH_REASON["code"])

    if qu in [i.upper() for i in hist_isins]:
        is_current = qu == (rec.get("current_isin") or "").upper()
        reasons.append(
            f"Matched {'current' if is_current else 'previous'} ISIN {qu}")
        signals["isin_continuity"] = True
        sources.append("AMFI NAVAll.txt (ISIN)")

    matched_hist_name = next(
        (n for n in hist_names if normalise_name(n) == qn or name_stem(n) == qs), None)
    if matched_hist_name and matched_hist_name != current_name:
        reasons.append(
            f"'{matched_hist_name}' is a previous name of this fund "
            f"(now '{current_name}')")
        sources.append("AMFI NAVAll.txt archived capture")
    elif matched_hist_name:
        reasons.append(_MATCH_REASON.get(matched_kind or "name", _MATCH_REASON["name"]))

    # Corporate actions that explain the identity change.
    actions = rec.get("corporate_actions", [])
    for a in actions:
        if a["type"] in (Action.NAME_CHANGE, Action.RECATEGORISATION):
            reasons.append(
                f"{a['type'].replace('_', ' ').title()} on {a.get('effective_date')}: "
                f"'{(a.get('from') or '')[:60]}' → '{(a.get('to') or '')[:60]}'")
        elif a["type"] == Action.AMC_CHANGE:
            reasons.append(
                f"Fund house changed on {a.get('effective_date')}: "
                f"{a.get('from')} → {a.get('to')}")
            signals["amc_continuity"] = True
        elif a["type"] == Action.MERGER:
            reasons.append(
                f"Merged into scheme {a.get('successor_code')} "
                f"({a.get('effective_date') or 'date unknown'})")
            signals["nav_correlation"] = a.get("source") == Source.CORRELATION
            signals["lifecycle"] = True
            if a.get("source") == Source.CURATED:
                signals["official_source"] = True
        elif a["type"] == Action.ISIN_CHANGE:
            reasons.append(
                f"ISIN changed on {a.get('effective_date')}: "
                f"{a.get('from')} → {a.get('to')}")
            signals["isin_continuity"] = True
        elif a["type"] == Action.WOUND_UP:
            reasons.append(f"Scheme stopped publishing NAV on {a.get('effective_date')}")

    former = None
    m = re.search(r"formerly\s+(?:known\s+as\s+)?(.+?)(?:\)|$)", current_name, re.IGNORECASE)
    if m:
        former = m.group(1).strip()
        reasons.append(f"AMFI records this scheme as formerly known as '{former}'")
        signals["official_source"] = True
        sources.append("AMFI scheme name ('Formerly Known as')")

    if rec.get("current_amc"):
        sources.append(f"AMFI AMC: {rec['current_amc']}")

    # ── Confidence depends on what KIND of claim this resolution is ──────────
    # Two different things get called "lineage", and scoring them the same way
    # is wrong:
    #
    #   SAME-CODE IDENTITY — the query matched a name or ISIN that AMFI itself
    #     recorded against THIS scheme code. Nothing is inferred; the registry
    #     said so. Confidence is 1.0, and the weighted evidence model does not
    #     apply (there is no succession being claimed). Scoring a directly
    #     observed Reliance→Nippon rename at 0.10 because it lacks "ISIN
    #     continuity" would understate a fact.
    #
    #   CROSS-CODE SUCCESSION — the query resolved THROUGH a predecessor or
    #     successor link, i.e. an assertion that two scheme codes are the same
    #     economic fund. That IS an inference, and it is scored by
    #     EVIDENCE_WEIGHTS from however many independent signals agree.
    crossed_codes = bool(
        (q.isdigit() and int(q) != rec.get("scheme_code"))
        or rec.get("predecessor_codes")
        or rec.get("successor_code")
    )
    if crossed_codes:
        resolution_type = "cross_code_succession"
        confidence = evidence_score(signals) if signals else None
        confidence_basis = "weighted evidence model (EVIDENCE_WEIGHTS)"
    else:
        resolution_type = "same_code_identity"
        confidence = 1.0
        confidence_basis = (
            "directly observed in AMFI's own record for this scheme code — "
            "no succession inferred"
        )

    return {
        "query": q,
        "resolution_type": resolution_type,
        "confidence_basis": confidence_basis,
        "resolved_to": {
            "scheme_code": rec.get("scheme_code"),
            "scheme_name": current_name,
            "isin": rec.get("current_isin"),
            "amc": rec.get("current_amc"),
            "active": rec.get("active"),
        },
        "formerly_known_as": former,
        "reasons": reasons or ["Matched the fund's current identity"],
        "signals": signals,
        "confidence": confidence,
        "sources": sorted(set(sources)) or ["AMFI NAVAll.txt"],
        "identity_history": {
            "names": hist_names,
            "isins": hist_isins,
            "amcs": [i["value"] for i in rec.get("amc_history", [])],
            "predecessor_codes": rec.get("predecessor_codes", []),
            "successor_code": rec.get("successor_code"),
        },
        "coverage": {
            "observed_from": rec.get("observed_from"),
            "observed_to": rec.get("observed_to"),
            "name_history_from": LINEAGE_COVERAGE_START.isoformat(),
            "caveat": (
                "Name history is derived from archived AMFI captures beginning "
                "2016; existence data reaches ~2006. Identity changes before "
                "those dates are not observable from these sources."
            ),
        },
        "lineage_version": rec.get("lineage_version"),
        "generator_version": rec.get("generator_version"),
        "unavailable_signals": UNAVAILABLE_SIGNALS,
        # Raw actions, dated — `reasons` above is prose for display; this is
        # what a caller building a year-picker (fund detail screen's AMC
        # History section, "select the year from which data should be
        # considered") needs instead of parsing dates back out of a sentence.
        "corporate_actions": actions,
    }


async def explain(db, query: str, scheme_code: int) -> Optional[Dict[str, Any]]:
    """DB-backed explanation for a single resolved fund."""
    rec = await db[LINEAGE_COLLECTION].find_one({"scheme_code": int(scheme_code)}, {"_id": 0})
    return explain_record(query, rec) if rec else None


def summarise(records: Dict[int, LineageRecord]) -> Dict[str, Any]:
    """Deliverable counts — funds audited, actions found, review backlog."""
    by_type: Dict[str, int] = defaultdict(int)
    for rec in records.values():
        for a in rec.corporate_actions:
            by_type[a["type"]] += 1
    return {
        "funds_audited": len(records),
        "active_funds": sum(1 for r in records.values() if r.active),
        "inactive_funds": sum(1 for r in records.values() if not r.active),
        "funds_with_isin": sum(1 for r in records.values() if r.current_isin),
        "corporate_actions_total": sum(by_type.values()),
        "by_action_type": dict(by_type),
        "funds_with_any_action": sum(1 for r in records.values() if r.corporate_actions),
        "lineage_mappings": sum(len(r.predecessor_codes) for r in records.values()),
        "requires_manual_review": sum(1 for r in records.values() if r.review),
        "coverage_start": LINEAGE_COVERAGE_START.isoformat(),
    }
