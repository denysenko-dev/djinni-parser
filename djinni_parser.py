"""
djinni_parser.py

Personal-use scraper for public Frontend (JS/React/TypeScript) job postings
on Djinni.co. Collects publicly visible vacancy listings and saves them to
a CSV file for personal job-search purposes only — not intended for
commercial use, resale, or redistribution of the collected data.

Compliance notes:
- Respects djinni.co/robots.txt, checked programmatically at runtime via
  urllib.robotparser (not a hardcoded path list) — see RobotsGate below.
- Only ever visits /jobs/<id>-<slug>/ and /jobs/?... listing/search pages.
- Uses a plain desktop-browser User-Agent and a randomized 2-4s delay
  between every request; makes no parallel/concurrent requests.
- Does not log in or use any authenticated session.
- Stops the whole run immediately on HTTP 429, a CAPTCHA/challenge page,
  or repeated timeouts, instead of retrying against a site that is
  signalling it wants the traffic to stop.

robots.txt is a technical courtesy signal to crawlers, not a substitute
for a site's Terms of Service. Review Djinni's ToS yourself before using
this script for anything beyond personal, non-commercial job searching.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
import time
import urllib.robotparser
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode, urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://djinni.co"
JOBS_PATH = "/jobs/"
PRIMARY_KEYWORD = "JavaScript/Frontend"
SEARCH_KEYWORDS = ["React", "Frontend", "JavaScript", "TypeScript"]
JOB_URL_RE = re.compile(r"^/jobs/(\d+)-[^/]+/$")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

CAPTCHA_MARKERS = (
    "captcha",
    "just a moment",
    "attention required",
    "cf-challenge",
    "are you human",
)

ENGLISH_LEVELS = (
    # CEFR letter codes ("English level B2+") first — unambiguous, so they
    # outrank a vaguer qualifier mentioned in the same sentence (e.g.
    # "Strong communication skills and English at B2 level" should read B2)
    "A1", "A2", "B1", "B2", "C1", "C2",
    # formal CEFR-ish taxonomy Djinni uses in its own filters
    # "Upper-Intermediate"/"Pre-Intermediate" must be checked before plain
    # "Intermediate" — that word is a substring of both, so the shorter form
    # would otherwise win and silently downgrade the real level
    "No English", "Beginner", "Elementary", "Pre-Intermediate",
    "Upper-Intermediate", "Intermediate", "Advanced", "Fluent", "Proficient", "Native",
    "Без англійської", "Початковий", "Елементарний", "Середній",
    "Вище середнього", "Просунутий",
    # informal qualifiers employers actually write in free-text descriptions
    "Strong", "Good", "Excellent", "Working", "Conversational", "Basic",
    "Вільна", "Впевнена", "Хороша", "Розмовна",
)

CSV_FIELDS = [
    "id", "title", "company", "url", "matched_keyword",
    "experience_level", "english_level", "work_format", "location",
    "salary_from", "salary_to", "salary_currency",
    "posted_date", "views_count", "responses_count",
    "description",
]


class BlockedError(Exception):
    """Raised when the site signals it wants the scraper to stop entirely."""


@dataclass
class Vacancy:
    id: str
    title: str | None
    company: str | None
    url: str
    matched_keyword: str
    experience_level: str | None
    english_level: str | None
    work_format: str | None
    location: str | None
    salary_from: float | None
    salary_to: float | None
    salary_currency: str | None
    posted_date: str | None
    views_count: int | None
    responses_count: int | None
    description: str | None


class RobotsGate:
    """Wraps urllib.robotparser so every request is checked against the
    live robots.txt instead of a hardcoded, possibly stale, path list."""

    def __init__(self, base_url: str, user_agent: str):
        self._rp = urllib.robotparser.RobotFileParser()
        self._rp.set_url(urljoin(base_url, "/robots.txt"))
        self._rp.read()
        self._user_agent = user_agent

    def allowed(self, url: str) -> bool:
        return self._rp.can_fetch(self._user_agent, url)


class DjinniScraper:
    def __init__(self, delay_range: tuple[float, float], timeout: int = 15):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.robots = RobotsGate(BASE_URL, USER_AGENT)
        self.delay_range = delay_range
        self.timeout = timeout
        self.errors = 0

    def _sleep(self) -> None:
        time.sleep(random.uniform(*self.delay_range))

    def fetch(self, url: str) -> str | None:
        if not self.robots.allowed(url):
            print(f"  [skip] disallowed by robots.txt: {url}")
            return None

        try:
            resp = self.session.get(url, timeout=self.timeout)
        except requests.exceptions.Timeout:
            print(f"  [error] timeout: {url}")
            self.errors += 1
            return None
        except requests.exceptions.RequestException as exc:
            print(f"  [error] request failed ({exc}): {url}")
            self.errors += 1
            return None
        finally:
            self._sleep()

        if resp.status_code == 429:
            raise BlockedError(f"HTTP 429 (rate limited) on {url}")
        if resp.status_code in (403, 503):
            raise BlockedError(f"HTTP {resp.status_code} (likely blocked/challenge) on {url}")
        if resp.status_code != 200:
            print(f"  [error] HTTP {resp.status_code}: {url}")
            self.errors += 1
            return None

        lowered = resp.text.lower()
        if any(marker in lowered for marker in CAPTCHA_MARKERS):
            raise BlockedError(f"CAPTCHA/challenge page detected on {url}")

        return resp.text

    # ---- listing pages -------------------------------------------------

    def collect_job_urls(self, keyword: str, max_pages: int) -> set[str]:
        found: set[str] = set()
        for page in range(1, max_pages + 1):
            params = {
                "primary_keyword": PRIMARY_KEYWORD,
                "all_keywords": keyword,
                "search_type": "full-text",
            }
            if page > 1:
                params["page"] = page
            url = f"{BASE_URL}{JOBS_PATH}?{urlencode(params)}"

            html = self.fetch(url)
            if html is None:
                break

            soup = BeautifulSoup(html, "html.parser")
            page_urls = {
                urljoin(BASE_URL, a["href"])
                for a in soup.find_all("a", href=JOB_URL_RE)
            }
            new_urls = page_urls - found
            print(f"  [{keyword}] page {page}: {len(page_urls)} listed, {len(new_urls)} new")

            if not page_urls:
                break
            found |= new_urls
            if not new_urls:
                # same links as before — pagination likely exhausted/looping
                break
        return found

    # ---- job detail pages ------------------------------------------------

    def parse_job(self, url: str, matched_keyword: str) -> Vacancy | None:
        html = self.fetch(url)
        if html is None:
            return None

        soup = BeautifulSoup(html, "html.parser")
        job_id = JOB_URL_RE.match(url[len(BASE_URL):]).group(1)  # type: ignore[union-attr]
        page_text = soup.get_text("\n", strip=True)  # only used for view/response counters

        data = self._extract_job_posting_ld(soup)
        if data is None:
            # Fallback for the rare page missing the structured JobPosting —
            # keep the vacancy with whatever the raw page text yields instead
            # of dropping it entirely.
            print(f"  [warn] no ld+json JobPosting found, using degraded parsing: {url}")
            title = self._safe(lambda: soup.find("h1").get_text(strip=True))
            return Vacancy(
                id=job_id, title=title,
                company=None, url=url, matched_keyword=matched_keyword,
                experience_level=self._experience_level(soup, title, None),
                english_level=self._english_level(soup, page_text),
                work_format=self._work_format(soup, {}, page_text),
                location=None, salary_from=None, salary_to=None, salary_currency=None,
                posted_date=None,
                views_count=self._count(page_text, r"(\d+)\s*перегляд"),
                responses_count=self._count(page_text, r"(\d+)\s*відгук"),
                description=page_text,
            )

        description = data.get("description") or page_text
        salary_from, salary_to, salary_currency = self._parse_salary(data.get("baseSalary"))

        return Vacancy(
            id=job_id,
            title=data.get("title"),
            company=self._company(data),
            url=url,
            matched_keyword=matched_keyword,
            experience_level=self._experience_level(
                soup, data.get("title"),
                (data.get("experienceRequirements") or {}).get("monthsOfExperience"),
            ),
            english_level=self._english_level(soup, description),
            work_format=self._work_format(soup, data, description),
            location=self._location(data),
            salary_from=salary_from,
            salary_to=salary_to,
            salary_currency=salary_currency,
            posted_date=(data.get("datePosted") or "")[:10] or None,
            views_count=self._count(page_text, r"(\d+)\s*перегляд"),
            responses_count=self._count(page_text, r"(\d+)\s*відгук"),
            description=description,
        )

    @staticmethod
    def _extract_job_posting_ld(soup: BeautifulSoup) -> dict | None:
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(tag.string or "")
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(data, dict) and data.get("@type") == "JobPosting":
                return data
        return None

    @staticmethod
    def _parse_salary(base_salary: dict | None) -> tuple[float | None, float | None, str | None]:
        if not base_salary:
            return None, None, None
        value = base_salary.get("value") or {}
        return value.get("minValue"), value.get("maxValue"), base_salary.get("currency")

    @staticmethod
    def _summary_card(soup: BeautifulSoup):
        """The right-rail summary card (experience/format/location/language
        facts, rendered as plain <li>/<strong> text with no distinguishing
        classes of their own) lives in its own "card card-body" div — a
        *different* one from wherever span.csc--language ends up on the page
        (their nesting isn't consistent across postings), so anchor on this
        card's own content ("досвід" always appears here) instead of walking
        up from another element. The card's li order and count also varies
        (e.g. a salary line sometimes appears in between), so position-based
        indexing isn't safe either — every lookup here matches by content."""
        for card in soup.find_all("div", class_="card"):
            classes = card.get("class") or []
            if "card-body" in classes and "досвід" in card.get_text(" ", strip=True).lower():
                return card
        return None

    @classmethod
    def _structured_experience_years(cls, soup: BeautifulSoup) -> float | None:
        card = cls._summary_card(soup)
        if card is None:
            return None
        for li in card.find_all("li"):
            lowered = li.get_text(" ", strip=True).lower()
            if "досвід" not in lowered:
                continue
            if "без досвіду" in lowered:
                return 0.0
            m = re.search(r"(\d+(?:[.,]\d+)?)\s*рок", lowered)
            if m:
                return float(m.group(1).replace(",", "."))
        return None

    @classmethod
    def _structured_work_format(cls, soup: BeautifulSoup) -> str | None:
        card = cls._summary_card(soup)
        if card is None:
            return None
        for li in card.find_all("li"):
            text = li.get_text(" ", strip=True)
            lowered = text.lower()
            if any(w in lowered for w in ("офіс", "віддалено", "гібрид")):
                return _normalize_structured_work_format(text)
        return None

    @classmethod
    def _experience_level(cls, soup: BeautifulSoup, title: str | None, months: float | None) -> str | None:
        years = cls._structured_experience_years(soup)
        if years is None and months is not None:
            years = months / 12
        return infer_experience_level(title or "", years)

    @classmethod
    def _work_format(cls, soup: BeautifulSoup, data: dict, description: str) -> str | None:
        structured = cls._structured_work_format(soup)
        if structured:
            return structured
        if data.get("jobLocationType") == "TELECOMMUTE":
            return "remote"
        return infer_work_format(description)

    @staticmethod
    def _extract_language_levels(soup: BeautifulSoup) -> dict[str, str]:
        """Djinni renders required language levels in a dedicated structured
        block (span.csc--language, e.g. "Англійська" / "A2 - Елементарний")
        that is often NOT echoed anywhere in the free-text job description —
        so a vacancy with no "English"/"англійська" mention in the description
        can still have an explicit level here. Scrape it directly instead of
        only guessing from text."""
        levels = {}
        for span in soup.find_all("span", class_="csc--language"):
            primary = span.find("span", class_="csc__primary")
            secondary = span.find("span", class_="csc__secondary")
            if primary and secondary:
                levels[primary.get_text(strip=True)] = secondary.get_text(strip=True)
        return levels

    @classmethod
    def _english_level(cls, soup: BeautifulSoup, description: str) -> str | None:
        for language, level in cls._extract_language_levels(soup).items():
            if "англ" in language.lower() or "english" in language.lower():
                return level
        # fall back to the free-text heuristic for the rare page where the
        # structured language block is missing
        return infer_english_level(description)

    @staticmethod
    def _company(data: dict) -> str | None:
        org = data.get("hiringOrganization")
        if isinstance(org, dict):
            return org.get("name")
        if isinstance(org, str):
            # employers can post as "confidential" instead of a named Organization
            return org or None
        return None

    @staticmethod
    def _location(data: dict) -> str | None:
        job_location = data.get("jobLocation")
        if isinstance(job_location, dict):
            job_location = [job_location]
        if not isinstance(job_location, list):
            return None

        parts = []
        for place in job_location:
            address = (place or {}).get("address") or {}
            locality = address.get("addressLocality")
            if isinstance(locality, list):
                locality = ", ".join(locality)
            country = address.get("addressCountry")
            text = ", ".join(p for p in (locality, country) if p)
            if text:
                parts.append(text)
        return "; ".join(parts) or None

    @staticmethod
    def _safe(getter):
        try:
            value = getter()
            return value or None
        except (AttributeError, TypeError):
            return None

    @staticmethod
    def _count(text: str, pattern: str) -> int | None:
        m = re.search(pattern, text, re.IGNORECASE)
        return int(m.group(1)) if m else None


# ---- text-parsing heuristics --------------------------------------------

def _contains_word(lowered_text: str, words: tuple[str, ...]) -> bool:
    """Whole-word match — a naive substring check would let "intern" match
    inside "internal"/"international", or "middle" inside "middleware"."""
    return re.search(r"\b(?:" + "|".join(re.escape(w) for w in words) + r")\b", lowered_text) is not None


def infer_experience_level(title: str, years: float | None = None) -> str | None:
    """Explicit junior/middle/senior wording wins over a numeric years
    threshold, since job titles like "Strong Junior" (3+ years required)
    disagree with a pure years cutoff.

    Only the TITLE is scanned for these words — not the description. The
    description routinely mentions OTHER people's seniority ("working
    alongside Senior developers who'll mentor you"), which has nothing to do
    with the role's own required level; scanning it caused real
    misclassifications (a "Trainee/Junior" posting read as "Senior" purely
    because it name-dropped senior mentors)."""
    lowered = (title or "").lower()
    if _contains_word(lowered, ("senior", "сеньйор", "старший")):
        return "Senior"
    if _contains_word(lowered, ("middle", "медіор", "мідл")):
        return "Middle"
    if _contains_word(lowered, ("trainee", "intern", "стажер", "стажист")):
        return "Trainee"
    if _contains_word(lowered, ("junior", "джуніор", "початків")):
        return "Junior"

    if years is not None:
        if years >= 5:
            return "Senior"
        if years >= 2:
            return "Middle"
        return "Junior"
    return None


_CEFR_CYRILLIC_LOOKALIKE_RE = re.compile(r"[АВС](?=[12]\b)")
_CEFR_CYRILLIC_LOOKALIKES = {"А": "A", "В": "B", "С": "C"}


def _normalize_cefr_codes(text: str) -> str:
    """Ukrainian-language postings sometimes write CEFR codes ("B1", "C1")
    with Cyrillic look-alike letters (Cyrillic А/В/С instead of Latin A/B/C)
    since the surrounding sentence is Cyrillic. Only touch a letter directly
    followed by "1"/"2" (the CEFR-code shape) — a blanket replacement would
    also corrupt ordinary Cyrillic words that start with the same letters
    (e.g. "Середній", "Вільна")."""
    return _CEFR_CYRILLIC_LOOKALIKE_RE.sub(
        lambda m: _CEFR_CYRILLIC_LOOKALIKES[m.group(0)], text
    )


def infer_english_level(text: str) -> str | None:
    """Best-effort only: employers write this as free text ("Strong English",
    "англійська B2"), not a fixed vocabulary, so this will miss phrasings
    outside ENGLISH_LEVELS. Descriptions often mention "English" more than
    once (e.g. "communicate in English" AND, separately, "English level B2+")
    — scan every mention, not just the first, and return the first one that
    actually carries a recognizable level."""
    for m in re.finditer(r"[^\n]{0,60}(?:english|англ[іi]йськ\w*)[^\n]{0,40}", text, re.IGNORECASE):
        window = _normalize_cefr_codes(m.group(0)).lower()
        for level in ENGLISH_LEVELS:
            if level.lower() in window:
                return level
    return None


def infer_work_format(text: str) -> str | None:
    lowered = text.lower()
    if "віддалено" in lowered or "remote" in lowered:
        return "remote"
    if "гібрид" in lowered or "hybrid" in lowered:
        return "hybrid"
    if "офіс" in lowered or "office" in lowered:
        return "office"
    return None


def _normalize_structured_work_format(text: str) -> str | None:
    """Djinni's own work-format field is a multi-select (e.g. "Офіс,
    Гібридний формат роботи" = office AND hybrid both acceptable) — collapse
    it to a comma-joined list of tags rather than picking just one, which
    would silently drop an accepted format."""
    lowered = text.lower()
    formats = []
    if "віддалено" in lowered:
        formats.append("remote")
    if "гібрид" in lowered:
        formats.append("hybrid")
    if "офіс" in lowered:
        formats.append("office")
    return ", ".join(formats) or None


# ---- CSV / resume support ------------------------------------------------

def load_seen_urls(csv_path: Path) -> set[str]:
    if not csv_path.exists():
        return set()
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        return {row["url"] for row in csv.DictReader(f)}


def open_writer(csv_path: Path):
    is_new = not csv_path.exists()
    f = csv_path.open("a", newline="", encoding="utf-8-sig")
    writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
    if is_new:
        writer.writeheader()
    return f, writer


# ---- main -----------------------------------------------------------------

def main() -> int:
    # some Windows consoles (cmd.exe/PowerShell with a legacy codepage) can't
    # print the emoji/dashes used below — fall back to '?' instead of crashing
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="vacancies_frontend.csv", type=Path)
    parser.add_argument("--max-pages", default=20, type=int, help="max listing pages per keyword")
    parser.add_argument("--delay-min", default=2.0, type=float)
    parser.add_argument("--delay-max", default=4.0, type=float)
    parser.add_argument("--dry-run", action="store_true", help="only collect+count job URLs, don't visit them")
    args = parser.parse_args()

    scraper = DjinniScraper(delay_range=(args.delay_min, args.delay_max))
    seen = load_seen_urls(args.output)
    if seen:
        print(f"Resuming: {len(seen)} vacancies already in {args.output}, will skip them.")

    url_keyword: dict[str, str] = {}
    try:
        for keyword in SEARCH_KEYWORDS:
            print(f"Searching keyword: {keyword}")
            urls = scraper.collect_job_urls(keyword, args.max_pages)
            print(f"  -> {len(urls)} unique job URLs for '{keyword}'")
            for u in urls:
                url_keyword.setdefault(u, keyword)  # first keyword to find it wins
    except BlockedError as exc:
        print(f"\n🛑 Site appears to be blocking requests during search: {exc}")
        print("Stopping now instead of retrying.")
        return 1

    to_process = sorted(set(url_keyword) - seen)
    print(f"\nTotal unique vacancies found: {len(url_keyword)} "
          f"({len(seen)} already saved, {len(to_process)} new to process)")

    if args.dry_run:
        print("Dry run — not fetching job detail pages.")
        return 0

    processed = 0
    csv_file, writer = open_writer(args.output)
    try:
        for i, url in enumerate(to_process, 1):
            print(f"[{i}/{len(to_process)}] {url}")
            try:
                vacancy = scraper.parse_job(url, matched_keyword=url_keyword[url])
            except BlockedError as exc:
                print(f"\n🛑 Site appears to be blocking requests: {exc}")
                print(f"Stopping now. Saved {processed} vacancies so far.")
                break
            if vacancy is None:
                continue
            writer.writerow({f: getattr(vacancy, f) for f in CSV_FIELDS})
            csv_file.flush()
            processed += 1
    finally:
        csv_file.close()

    print(f"\nDone. Processed: {processed}, errors/skipped: {scraper.errors}, "
          f"output: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
