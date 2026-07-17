# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-file, personal-use scraper (`djinni_parser.py`) that collects public Frontend (React/JavaScript/TypeScript) job postings from Djinni.co into a CSV, for the repo owner's own job search — not for commercial use, resale, or redistribution of Djinni's content.

## Commands

```
python -m venv .venv && .venv\Scripts\activate && pip install -r requirements.txt

python djinni_parser.py --dry-run              # count vacancies, fetch no detail pages
python djinni_parser.py                        # full run into vacancies_frontend.csv
python djinni_parser.py --max-pages 5           # cap pagination depth per search keyword
python djinni_parser.py --output my.csv         # different output file
python djinni_parser.py --delay-min 2 --delay-max 4   # per-request delay range (seconds)
python djinni_parser.py --start-page 15 --max-pages 20   # resume search pagination without re-walking earlier pages
```

There is no test suite, linter, or build step — this is one script plus `requirements.txt`. Ad-hoc verification during development is done by importing functions directly and running them against real fetched pages or rows from the existing CSV, e.g.:

```
python -c "from djinni_parser import classify_category; print(classify_category('Senior Frontend Developer', 'React, TypeScript'))"
```

When changing any HTML-parsing logic, verify against a live page fetch before trusting it — Djinni's actual markup has repeatedly turned out to differ from what schema.org / the visible page structure implies (see below). Re-run against a sample of rows in `vacancies_frontend.csv` (real descriptions already on disk) before assuming a regex fix is correct.

## Architecture

### Three-tier data source, in priority order

Every extracted field prefers the most structured source available and falls back only when it's missing. This ordering is the single most important thing to preserve when touching any field:

1. **`<script type="application/ld+json">` JobPosting block** (`_extract_job_posting_ld`) — `title`, `company`, `description`, `posted_date`, `salary_from/to`, `experienceRequirements`, etc. Parsed once per page and threaded through `parse_job()`.
2. **Dedicated structured HTML elements elsewhere on the page** — used when ld+json doesn't carry a field, or carries it unreliably:
   - `english_level` comes from `span.csc--language` (a "Вимоги до володіння мовами" widget), *not* the free-text description — free-text guessing left ~60% of vacancies "unspecified"; the structured field gets that down to ~9%.
   - `experience_level`/`work_format` come from a right-rail summary card: `_summary_card()` finds it by scanning all `div.card.card-body` elements for the word "досвід" in their text — **not** by walking up from `span.csc--language`, because that language span and the summary card are *not* reliably nested inside each other across postings. Li order/count inside that card also varies (a salary line sometimes appears in between), so every lookup inside it matches by content, never by position.
3. **Free-text regex heuristics over the description** (`infer_english_level`, `infer_work_format`, `infer_experience_level`) — last-resort fallback only, kept for the rare page missing the structured element above.

### ld+json fields are not trustworthy to `.get()` blindly

Several ld+json fields have turned out to deviate from what schema.org implies, each once causing a crash that killed a full run before being fixed:
- `hiringOrganization` can be the literal string `"confidential"` instead of an `Organization` dict (`_company`, `_industry`).
- `jobLocation` can be a list of `Place` objects instead of a single dict (`_location`).
- `baseSalary` / `experienceRequirements` are defensively `isinstance`-checked (`_parse_salary`, `_months_of_experience`) rather than assumed to be dicts, on the same suspicion.

`main()`'s per-vacancy loop also catches broad `Exception` (not just the site-signaling `BlockedError`) around `parse_job()`, so a future unanticipated shape costs one vacancy, not the whole run's progress.

### Request layer (`DjinniScraper` / `RobotsGate`)

- `RobotsGate` checks `djinni.co/robots.txt` live (via `urllib.robotparser`) rather than a hardcoded path list.
- `fetch()` re-checks robots.txt against `resp.url` (the post-redirect URL), not just the requested URL, since `requests` follows redirects automatically.
- `fetch()` retries timeouts and 5xx with exponential backoff (up to 3 attempts); HTTP 429/403/503 and a CAPTCHA-marker match in the response body raise `BlockedError` immediately and are *never* retried — those mean the site wants traffic to stop, not that one request had bad luck. `BlockedError` propagates up to `main()`, which stops the whole run and preserves whatever was already flushed to CSV.
- A single 2-4s randomized delay is applied per real HTTP attempt; no concurrent requests.

### `parse_job()` has two branches

The happy path (ld+json present) and a degraded path (rare pages missing the `JobPosting` block — falls back to `<h1>` for title and raw page text for everything else). Both branches independently call the same field-resolution helpers (`_resolve_experience_level`, `_english_level`, `_work_format`, etc.) and both wrap the newer "extended fields" (see below) in their own `try/except`, defaulting those specific fields to `None` on failure without losing the rest of the vacancy.

### `classify_category` / `extract_tech_stack` / `split_requirements`

Best-effort text classifiers, each with deliberate, documented edge-case handling worth knowing before changing:
- Role-exclusion keywords (QA/Support/SEO/PM/...) in `classify_category` are matched only against the **primary segment of the title**, stripped of any trailing `" - <suffix>"` (`_TITLE_SUFFIX_RE`) — real Djinni titles append a duty suffix like `"Full-Stack Engineer (NestJS, Angular) - On-Call Support"`, and matching exclusion words anywhere in the title or description caused real Frontend/Fullstack postings to be misclassified as `"Other"`.
- React Native is explicitly bucketed as `"Other"`, not `"Frontend"` — a deliberate choice (mobile, not web front-end), not a keyword-matching accident.
- Nothing is ever dropped from the output for being off-category; `"Other"` is a visible label so keyword-search noise (QA/PM/SEO/pure-backend postings that matched the search terms incidentally) stays inspectable rather than silently vanishing.

### CSV output / resume / schema migration

- `CSV_FIELDS` (module-level list) and the `Vacancy` dataclass must be kept in lockstep — `main()` writes rows via `{f: getattr(vacancy, f) for f in CSV_FIELDS}`.
- `load_seen_urls()` reads existing `url` values from the output CSV so re-running the same command only fetches new vacancies (safe to interrupt and resume).
- `open_writer()` calls `_migrate_csv_if_needed()` first: if the existing CSV's header doesn't match the current `CSV_FIELDS` (i.e. the schema grew since that file was written), it rewrites the whole file with the new header, preserving old rows' data and leaving new columns empty — appending straight onto an old-header file would otherwise silently misalign columns.

### Search phase

`collect_job_urls()` paginates Djinni's search per keyword in `SEARCH_KEYWORDS` (currently `["React", "Frontend", "JavaScript", "TypeScript"]`) up to `--max-pages`, starting from `--start-page` (default 1), stopping early on an empty page or a page with zero new URLs (pagination exhausted/looping). `--start-page` exists because every run otherwise re-walks pages 1..N from scratch for every keyword regardless of what a prior run already covered — on a site that appears to block after a roughly fixed number of *requests* in a session rather than by request rate (raising `--delay-min/--delay-max` alone didn't move the block point in testing), that wasted budget on already-seen pages was the main reason a resumed run kept hitting the block before reaching new territory. Trade-off: Djinni's page-1 ordering can shift between runs as new postings land, so a high `--start-page` can skip a handful of newly-landed listings that would now sort into an earlier page — acceptable for resuming a large collection, not for a single from-scratch run. Combining all four keywords into a single query was tested and rejected for now — the result set differs too much from a single-keyword search (~1/15 overlap) to trust without confirming Djinni's actual match semantics, and a wrong guess would silently under-collect with no error raised (see the `SEARCH_KEYWORDS` comment).
