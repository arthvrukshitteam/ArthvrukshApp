# Performance audit

Static review plus one micro-benchmark. **No production traces, no profiler, no load test** were available — anything about live latency/RAM is UNKNOWN. Target environment per code comments: Render free instance, **512 MB RAM**, one Uvicorn worker.

## Measured (synthetic, this research pass)

`analyze_fund()` on a random-walk daily NAV series (weekday NAVs, Flexi Cap, Python 3.11, Apple-silicon laptop) — indicative only:

| History | NAV rows | Horizons with data | Total windows | Time (with windows) | Time (`include_windows=False`) | JSON size (with windows) |
|---|---|---|---|---|---|---|
| 5 y | 1 304 | 4 | 116 | ~0.00 s | ~0.00 s | 24 KB |
| 15 y | 3 912 | 14 | 1 246 | 0.03 s | 0.02 s | 245 KB |
| 25 y | 6 521 | 24 | 3 576 | 0.12 s | 0.09 s | **697 KB** |

Takeaways: the engine itself is cheap (≈0.1 s even for a 25-year fund), so event-loop blocking in `_build_analysis` is a *minor* issue per request; the real costs are **network fetches to mfapi.in** (45 s timeout each, plus predecessor/successor fetches), **payload/document size** (≈0.7 MB per long-history fund, returned to the phone and stored in `fund_analysis`), and **batch volume** (~12 k funds × several fetches).

## Findings

| # | Area | Finding | Impact | Suggested direction |
|---|---|---|---|---|
| P1 | API payload | `/funds/{c}/analyze` returns every window of every horizon (up to ~0.7 MB+ for 25-y funds; ≈1 MB at 30 y) even though the UI shows one horizon at a time with 30-row pages. | Slow on mobile networks, big Mongo docs, wasted parse time in JS | Return windows lazily per horizon (`?horizon=`), or paginate windows |
| P2 | API payload | `/home/universe` returns **all** eligible funds (thousands) in one JSON with no pagination, ETag or server-side filtering; filter chips and search are client-side. | Memory + parse cost on low-end Android; refetch on every focus/toggle | Cache by `(mode, tenure, toggles, scores_last_refreshed)`; paginate/filter server-side or send a compact column format |
| P3 | DB | Universe queries stream with `batch_size=200` and trimmed projections (good), but each request still reads every eligible `fund_nav_meta` doc; the second (Additional Recs) query loads `fqs_by_horizon(+2010+continuity)` unconditionally for candidates. Indexes: `scheme_code`, `(history_years, own_latest_nav_date)`, `latest_nav_date` only. | Per-request CPU/IO grows with universe; `superseded_by`/`merged_with` clauses in `active_query` are not index-assisted | Precompute the per-tenure ranked lists in the nightly job (or cache for the day) |
| P4 | DB | `fund_analysis` and `fund_lumpsum_analysis` are looked up by `{scheme_code, start_date_applied:null}` / `$in scheme_code` with **no declared index**; `fund_recent` sorted on `last_seen` with no index; `fund_identifier_idx` unanchored `$regex` on `key` for the name-stem fallback scans all stem rows. | Collection scans as data grows (cheap today, hundreds of docs) | Add indexes (`scheme_code`, `{scheme_code,start_date_applied}`, `last_seen`); anchor/prefix the regex |
| P5 | Search | Fallback layer iterates the in-memory AMFI list (~12 k rows) in Python per request; `limit*6` candidates are status-annotated with one `$in`. | Fine today; CPU-bound at scale | Index names in Mongo (text/prefix) |
| P6 | N+1 / fan-out | Rolling ranking: up to 80 live NAV fetches per click (concurrency 8). Bulk review: per-row `match_fund` + possible live `_build_analysis`. Proposal build: one fund at a time sequentially (`[await _fund_row(...) for f in funds]`, ≤10). | Latency of seconds–minutes; mfapi.in rate-limit exposure (no handling/backoff) | Reuse `fund_nav_meta`/NAV cache; bounded gather in proposals |
| P7 | Batch jobs | Nightly FQS = per fund: 1+ network fetch (+1 for predecessor funds) + 1–3 `analyze_fund` thread calls; whole universe sequentially paged (concurrency 6). Lumpsum 4. The two jobs are serialised by `batch_support`, so wall-clock ≈ sum. | Multi-hour runs inside the web process; killed by restarts (free tier) | Move to a worker/cron service; checkpoint progress; skip funds whose latest NAV date hasn't changed |
| P8 | Redundant NAV fetching | The same fund's NAV is re-downloaded in full by: nightly FQS, nightly lumpsum, daily refresh, any analyze cache miss, proposal/bulk fallback, crawl. No shared NAV cache (only the master list is cached 12 h). | 3–5× upstream traffic per fund per night | Cache raw NAV payload (Mongo/disk) keyed by `(code, latest_date)` |
| P9 | Memory | Good defensive work already (keyset paging, projections, per-page release, `to_thread`, read-only openpyxl). Remaining peaks: `_FUND_LIST_CACHE` (~12 k dicts), full NAV payloads for stitched funds (predecessor + successor lists), 10–40 MB uploads held as `bytes` + base64 copies (base64 route decodes into a second copy), PDF/XLSX in-memory builds, `export/all-funds` building a list of every doc. | OOM risk on 512 MB when jobs overlap | Streaming responses; cap concurrent heavy jobs globally (only FQS/lumpsum are serialised today) |
| P10 | Event loop | `_build_analysis` (interactive and `_daily_refresh`) runs `analyze_fund` inline; `/refresh/run` is a **long synchronous HTTP request** (up to 20 min in CI); merger discovery pre-filters pairs in pure Python on the loop. | Short stalls (~0.1 s/fund interactive; `_daily_refresh` runs 4 at a time so stalls repeat) | `asyncio.to_thread` like the batch code does |
| P11 | Frontend | One big `FlatList` for Home with many `useMemo` sorts; Fund Detail windows table paginated; no image/asset issues; no lazy route splitting; unused heavy deps in `package.json` (gifted-charts, webview, video, blur, haptics, symbols) inflate install/upgrade surface (tree-shaking may drop them from the bundle — **not measured**). | Startup/bundle size UNKNOWN | Remove unused deps; analyse bundle |
| P12 | Frontend networking | No request timeout/abort, no retry, no de-duplication; leaving Home mid-request does not cancel it; Fund Detail refetches on every mount. | Wasted data, race conditions on fast toggle flips (`load` closure keyed by toggles; last response wins) | AbortController + a small query cache (e.g. TanStack Query) |
| P13 | Staleness | `fund_nav_meta` span fields never refresh (technical-debt #1) — correctness, but also means eligibility filters read frozen numbers. | – | See roadmap P0-2 |

## Hot paths, in order of user impact
1. Home universe (every launch / toggle / tenure change).
2. Fund detail analyze on cache miss (mfapi fetch + compute + 0.2–0.7 MB response).
3. Search (lineage + fallback + status annotation).
4. Proposal build (sequential per-fund analysis; plus PDF render).
5. Nightly jobs (hours; compete with all of the above for 512 MB).
