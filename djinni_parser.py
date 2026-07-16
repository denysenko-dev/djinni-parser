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
# NOTE: could combine all four into one all_keywords query to cut request
# volume — tested this and it returns a materially different result set
# (~1/15 overlap with a single-keyword search on page 1) but the exact
# match semantics (OR-union vs something narrower) aren't confirmed, and a
# wrong guess would silently under-collect vacancies with no error raised.
# Left as four separate searches until that's verified against the site.
SEARCH_KEYWORDS = ["React", "Frontend", "JavaScript", "TypeScript"]
JOB_URL_RE = re.compile(r"^/jobs/(\d+)-[^/]+/$")

# Deliberately a real desktop-browser string rather than a self-identifying
# bot UA (e.g. "MyScraper/1.0 (+contact)") — trade-off: this makes the
# scraper indistinguishable from a human visitor in Djinni's logs (so they
# can't selectively rate-limit or contact this script's operator), but a
# labelled bot UA tends to get blocked more readily, which would defeat the
# personal-use goal. robots.txt compliance and rate limiting are the actual
# courtesy mechanisms here, not UA transparency.
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

# Rough fixed rates for salary_usd_normalized — not live rates, just enough
# to sort/filter salaries roughly on the same scale. Update as needed.
FX_TO_USD = {
    "USD": 1.0,
    "UAH": 1 / 42.0,
    "EUR": 1.08,
}

CSV_FIELDS = [
    "id", "title", "company", "url", "matched_keyword",
    "category", "tech_stack",
    "experience_level", "english_level", "work_format", "location",
    "employment_type", "industry",
    "salary_from", "salary_to", "salary_currency", "salary_usd_normalized",
    "posted_date", "valid_through", "views_count", "responses_count",
    "must_have", "nice_to_have",
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
    category: str | None
    tech_stack: str | None
    experience_level: str | None
    english_level: str | None
    work_format: str | None
    location: str | None
    employment_type: str | None
    industry: str | None
    salary_from: float | None
    salary_to: float | None
    salary_currency: str | None
    salary_usd_normalized: float | None
    posted_date: str | None
    valid_through: str | None
    views_count: int | None
    responses_count: int | None
    must_have: str | None
    nice_to_have: str | None
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

    def fetch(self, url: str, max_attempts: int = 3) -> str | None:
        """Fetch a URL, retrying transient failures (timeout, 5xx) with
        exponential backoff — a single flaky response used to permanently
        skip that vacancy or truncate a keyword's pagination. 429/403/503
        are NOT retried: those mean the site wants traffic to stop, not that
        this one request had bad luck."""
        if not self.robots.allowed(url):
            print(f"  [skip] disallowed by robots.txt: {url}")
            return None

        for attempt in range(1, max_attempts + 1):
            resp = None
            try:
                resp = self.session.get(url, timeout=self.timeout)
            except requests.exceptions.Timeout:
                if attempt == max_attempts:
                    print(f"  [error] timeout (after {max_attempts} attempts): {url}")
                    self.errors += 1
                    return None
                print(f"  [retry {attempt}/{max_attempts - 1}] timeout: {url}")
            except requests.exceptions.RequestException as exc:
                print(f"  [error] request failed ({exc}): {url}")
                self.errors += 1
                return None
            finally:
                self._sleep()

            if resp is None:
                time.sleep(2 ** (attempt - 1))
                continue

            if resp.status_code == 429:
                raise BlockedError(f"HTTP 429 (rate limited) on {url}")
            if resp.status_code in (403, 503):
                raise BlockedError(f"HTTP {resp.status_code} (likely blocked/challenge) on {url}")
            if resp.status_code >= 500:
                if attempt == max_attempts:
                    print(f"  [error] HTTP {resp.status_code} (after {max_attempts} attempts): {url}")
                    self.errors += 1
                    return None
                print(f"  [retry {attempt}/{max_attempts - 1}] HTTP {resp.status_code}: {url}")
                time.sleep(2 ** (attempt - 1))
                continue
            if resp.status_code != 200:
                print(f"  [error] HTTP {resp.status_code}: {url}")
                self.errors += 1
                return None

            # requests follows redirects automatically — the robots.txt check
            # above only covers the URL we asked for, so re-check the URL we
            # actually landed on in case a redirect sent us somewhere disallowed.
            if resp.url != url and not self.robots.allowed(resp.url):
                print(f"  [skip] redirect target disallowed by robots.txt: {resp.url}")
                return None

            lowered = resp.text.lower()
            if any(marker in lowered for marker in CAPTCHA_MARKERS):
                raise BlockedError(f"CAPTCHA/challenge page detected on {url}")

            return resp.text

        return None

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
            try:
                category = classify_category(title, page_text)
                tech_stack = "; ".join(extract_tech_stack(page_text)) or None
                must_have, nice_to_have = split_requirements(page_text)
            except Exception as exc:
                # These are newer, best-effort-only fields — a bug in any one
                # of them should cost only itself, not the whole vacancy that
                # the fields above already parsed successfully.
                print(f"  [warn] failed to extract extended fields for {url}: {exc}")
                category = tech_stack = must_have = nice_to_have = None
            return Vacancy(
                id=job_id, title=title,
                company=None, url=url, matched_keyword=matched_keyword,
                category=category, tech_stack=tech_stack,
                experience_level=self._resolve_experience_level(soup, title, None),
                english_level=self._english_level(soup, page_text),
                work_format=self._work_format(soup, {}, page_text),
                location=None,
                employment_type=None, industry=None,
                salary_from=None, salary_to=None, salary_currency=None, salary_usd_normalized=None,
                posted_date=None, valid_through=None,
                views_count=self._count(page_text, r"(\d+)\s*перегляд"),
                responses_count=self._count(page_text, r"(\d+)\s*відгук"),
                must_have=must_have, nice_to_have=nice_to_have,
                description=page_text,
            )

        description = data.get("description") or page_text
        salary_from, salary_to, salary_currency = self._parse_salary(data.get("baseSalary"))
        counter_text = self._counter_text(page_text, description)
        title = data.get("title")

        try:
            category = classify_category(title, description)
            tech_stack = "; ".join(extract_tech_stack(description)) or None
            employment_type = self._employment_type(data)
            valid_through = str(data.get("validThrough") or "")[:10] or None
            industry = self._industry(data)
            must_have, nice_to_have = split_requirements(description)
            salary_usd_normalized = normalize_salary_usd(
                salary_to if salary_to is not None else salary_from, salary_currency
            )
        except Exception as exc:
            # Same isolation as the degraded-parsing branch above — one of
            # these newer fields misbehaving shouldn't cost the vacancy.
            print(f"  [warn] failed to extract extended fields for {url}: {exc}")
            category = tech_stack = employment_type = valid_through = None
            industry = must_have = nice_to_have = salary_usd_normalized = None

        return Vacancy(
            id=job_id,
            title=title,
            company=self._company(data),
            url=url,
            matched_keyword=matched_keyword,
            category=category, tech_stack=tech_stack,
            experience_level=self._resolve_experience_level(
                soup, title, self._months_of_experience(data),
            ),
            english_level=self._english_level(soup, description),
            work_format=self._work_format(soup, data, description),
            location=self._location(data),
            employment_type=employment_type, industry=industry,
            salary_usd_normalized=salary_usd_normalized,
            valid_through=valid_through,
            must_have=must_have, nice_to_have=nice_to_have,
            salary_from=salary_from,
            salary_to=salary_to,
            salary_currency=salary_currency,
            posted_date=(data.get("datePosted") or "")[:10] or None,
            views_count=self._count(counter_text, r"(\d+)\s*перегляд"),
            responses_count=self._count(counter_text, r"(\d+)\s*відгук"),
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
    def _parse_salary(base_salary) -> tuple[float | None, float | None, str | None]:
        # ld+json fields on this site have twice turned out to be a
        # different shape than schema.org's spec implies (jobLocation and
        # hiringOrganization were each once a list/str instead of a dict) —
        # guard baseSalary the same way rather than assuming .get() is safe.
        if not isinstance(base_salary, dict):
            return None, None, None
        value = base_salary.get("value")
        if not isinstance(value, dict):
            value = {}
        return value.get("minValue"), value.get("maxValue"), base_salary.get("currency")

    @staticmethod
    def _months_of_experience(data: dict) -> float | None:
        requirements = data.get("experienceRequirements")
        if not isinstance(requirements, dict):
            return None
        return requirements.get("monthsOfExperience")

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
    def _resolve_experience_level(cls, soup: BeautifulSoup, title: str | None, months: float | None) -> str | None:
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
    def _industry(data: dict) -> str | None:
        # hiringOrganization is a dict OR the string "confidential" (see
        # _company above) — isinstance-guard the same way before .get()-ing.
        org = data.get("hiringOrganization")
        if not isinstance(org, dict):
            return None
        industry = org.get("industry")
        return industry if isinstance(industry, str) and industry else None

    @staticmethod
    def _employment_type(data: dict) -> str | None:
        employment_type = data.get("employmentType")
        if isinstance(employment_type, list):
            return ", ".join(str(x) for x in employment_type if x) or None
        if isinstance(employment_type, str):
            return employment_type or None
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

    @staticmethod
    def _counter_text(page_text: str, description: str | None) -> str:
        """views_count/responses_count are extracted by regex over the whole
        rendered page (no stable CSS selector found for that widget) — strip
        the job description out first, since a coincidental "N відгуків"
        written inside the description itself (e.g. a company bragging about
        "500 відгуків клієнтів") would otherwise be mistaken for Djinni's own
        applicant-response counter."""
        if description:
            return page_text.replace(description, "")
        return page_text


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


# ---- category / tech stack / requirements split --------------------------

_REACT_NATIVE_RE = re.compile(r"\breact[\s-]?native\b", re.IGNORECASE)
_NON_DEV_ROLE_RE = re.compile(
    r"\b(?:qa|quality\s+assurance|support|seo|hr|recruiter|talent\s+acquisition|"
    r"sales|marketing|devops|business\s+analyst|project\s+manager|"
    r"product\s+manager|scrum\s+master|delivery\s+manager|"
    r"data\s+(?:engineer|scientist|analyst)|technical\s+writer|"
    r"ui[\s/]?ux\s+designer)\b",
    re.IGNORECASE,
)
_FRONTEND_FRAMEWORK_RE = re.compile(r"\b(?:react|vue|angular)\b", re.IGNORECASE)
_FRONTEND_ROLE_RE = re.compile(r"\bfront[\s-]?end\b", re.IGNORECASE)
_FULLSTACK_ROLE_RE = re.compile(r"\bfull[\s-]?stack\b", re.IGNORECASE)
_BACKEND_ROLE_RE = re.compile(r"\bback[\s-]?end\b", re.IGNORECASE)
# Djinni titles often append a duty/scope suffix after a dash, e.g. "Full-
# Stack Engineer (NestJS, Angular) - On-Call Support" — strip that trailing
# " - <suffix>" before checking for exclusion keywords, so a real Fullstack
# posting like that one doesn't get misread as a Support role just because
# its own title happens to mention an on-call duty after the dash.
_TITLE_SUFFIX_RE = re.compile(r"\s+-\s+.+$")


def classify_category(title: str | None, description: str | None) -> str:
    """"Frontend" | "Fullstack" | "Other" — vacancies that don't fit are
    tagged "Other" rather than dropped, so a keyword-search run over noisy
    real-world results stays visible instead of silently vanishing.

    Three decisions worth calling out:

    - Role-type exclusions (QA/support/SEO/PM/...) are checked against the
      PRIMARY title segment only (before any " - <suffix>"), never the
      description. The description is free text that can mention something
      like "on-call support rotation" as a duty of an otherwise plainly
      Frontend role — matching that substring anywhere in the body used to
      mislabel real Frontend postings as "Other". Real titles also append
      a duty suffix after a dash ("Full-Stack Engineer (NestJS, Angular) -
      On-Call Support") that must be excluded the same way, or the primary
      role name gets overridden by its own suffix.
    - React Native is explicitly bucketed as "Other", not "Frontend": it
      shares the "React" keyword but targets mobile apps, not the web —
      "Frontend" here means web front-end specifically. This is a
      deliberate choice, not a side effect of keyword matching.
    """
    title_l = (title or "").lower()
    desc_l = (description or "").lower()
    combined = f"{title_l}\n{desc_l}"
    primary_title_l = _TITLE_SUFFIX_RE.sub("", title_l)

    if _REACT_NATIVE_RE.search(combined):
        return "Other"

    if _NON_DEV_ROLE_RE.search(primary_title_l):
        return "Other"

    has_frontend_framework = bool(_FRONTEND_FRAMEWORK_RE.search(combined))
    is_fullstack_title = bool(_FULLSTACK_ROLE_RE.search(primary_title_l))

    if is_fullstack_title and has_frontend_framework:
        return "Fullstack"

    # "strong backend focus" — an explicit backend title that isn't also a
    # fullstack title reads as backend-only, even if the description happens
    # to name-drop a frontend framework used by some adjacent team.
    is_backend_only_title = bool(_BACKEND_ROLE_RE.search(primary_title_l)) and not is_fullstack_title

    # "front-end"/"frontend" as a bare word is only trusted from the TITLE —
    # in the description it's too easy to catch it in an unrelated or even
    # negating sentence ("no frontend work involved"). A named framework
    # (React/Vue/Angular) is a strong enough signal to trust from either.
    if not is_backend_only_title and (_FRONTEND_ROLE_RE.search(primary_title_l) or has_frontend_framework):
        return "Frontend"

    return "Other"


TECH_STACK_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("React", re.compile(r"\breact(?:\.js)?\b(?!\s*native)", re.IGNORECASE)),
    ("React Native", _REACT_NATIVE_RE),
    ("TypeScript", re.compile(r"\btypescript\b", re.IGNORECASE)),
    ("JavaScript", re.compile(r"\bjavascript\b", re.IGNORECASE)),
    ("Node.js", re.compile(r"\bnode(?:\.js)?\b", re.IGNORECASE)),
    ("Next.js", re.compile(r"\bnext(?:\.js)?\b", re.IGNORECASE)),
    ("Vue", re.compile(r"\bvue(?:\.js)?\b", re.IGNORECASE)),
    ("Angular", re.compile(r"\bangular(?:js)?\b", re.IGNORECASE)),
    ("Redux", re.compile(r"\bredux\b", re.IGNORECASE)),
    ("REST API", re.compile(r"\brest(?:ful)?\s*api\b", re.IGNORECASE)),
    ("GraphQL", re.compile(r"\bgraphql\b", re.IGNORECASE)),
    ("HTML/CSS", re.compile(r"\b(?:html5?|css3?)\b", re.IGNORECASE)),
    ("Tailwind", re.compile(r"\btailwind(?:\s*css)?\b", re.IGNORECASE)),
    ("Sass/SCSS", re.compile(r"\b(?:sass|scss)\b", re.IGNORECASE)),
    ("Docker", re.compile(r"\bdocker\b", re.IGNORECASE)),
    ("CI/CD", re.compile(r"\bci[\s/-]?cd\b", re.IGNORECASE)),
    ("Git", re.compile(r"\bgit\b", re.IGNORECASE)),
    ("Jest", re.compile(r"\bjest\b", re.IGNORECASE)),
    ("Playwright", re.compile(r"\bplaywright\b", re.IGNORECASE)),
    ("Cypress", re.compile(r"\bcypress\b", re.IGNORECASE)),
    ("Webpack", re.compile(r"\bwebpack\b", re.IGNORECASE)),
    ("Vite", re.compile(r"\bvite\b", re.IGNORECASE)),
    ("Agile/Scrum", re.compile(r"\b(?:agile|scrum)\b", re.IGNORECASE)),
    ("Figma", re.compile(r"\bfigma\b", re.IGNORECASE)),
    ("SSR", re.compile(r"\bssr\b|\bserver-side rendering\b", re.IGNORECASE)),
    ("Storybook", re.compile(r"\bstorybook\b", re.IGNORECASE)),
    ("MobX/Zustand", re.compile(r"\b(?:mobx|zustand)\b", re.IGNORECASE)),
    ("AI tools", re.compile(r"\b(?:cursor|copilot|claude)\b", re.IGNORECASE)),
)


def extract_tech_stack(description: str | None) -> list[str]:
    """Scan the free-text description for a fixed vocabulary of technology
    keywords (whole-word regex, case-insensitive) and return the matched
    display names, sorted alphabetically. Best-effort only — a niche tool
    outside this list simply won't show up."""
    if not description:
        return []
    return sorted(name for name, pattern in TECH_STACK_PATTERNS if pattern.search(description))


_MUST_HAVE_HEADER_RE = r"вимоги|requirements?|must[\s-]?have"
_NICE_TO_HAVE_HEADER_RE = r"буде\s+перевагою|плюсом\s+буде|nice[\s-]?to[\s-]?have|will\s+be\s+a\s+plus"
_SECTION_HEADER_RE = re.compile(
    rf"(?im)^[ \t]*(?:(?P<must>{_MUST_HAVE_HEADER_RE})|(?P<nice>{_NICE_TO_HAVE_HEADER_RE}))\s*:?\s*$"
)


def split_requirements(description: str | None) -> tuple[str | None, str | None]:
    """Best-effort split on common Ukrainian/English requirement-section
    headers standing alone on their own line ("Вимоги"/"Requirements"/"Must
    have" -> must_have; "Буде перевагою"/"Nice to have"/"Will be a plus"/
    "Плюсом буде" -> nice_to_have). A section runs from its header to the
    next recognized header (of either kind) or the end of the text.

    If no header is found, both fields stay None — description keeps the
    full original text untouched either way, so nothing is ever lost."""
    if not description:
        return None, None

    matches = list(_SECTION_HEADER_RE.finditer(description))
    if not matches:
        return None, None

    must_parts: list[str] = []
    nice_parts: list[str] = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(description)
        section_text = description[start:end].strip()
        if not section_text:
            continue
        (must_parts if m.group("must") else nice_parts).append(section_text)

    return "\n".join(must_parts) or None, "\n".join(nice_parts) or None


def normalize_salary_usd(amount: float | None, currency: str | None) -> float | None:
    """Rough fixed-rate conversion to USD (see FX_TO_USD) for cross-currency
    comparison — not live rates, just enough to sort/filter roughly on the
    same scale. Unknown currencies return None rather than guessing."""
    if amount is None or not currency:
        return None
    rate = FX_TO_USD.get(currency.upper())
    if rate is None:
        return None
    return round(amount * rate, 2)


# ---- CSV / resume support ------------------------------------------------

def load_seen_urls(csv_path: Path) -> set[str]:
    if not csv_path.exists():
        return set()
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        return {row["url"] for row in csv.DictReader(f)}


def _migrate_csv_if_needed(csv_path: Path) -> None:
    """CSV_FIELDS has grown over time (category/tech_stack/etc. added most
    recently). Appending new rows straight onto an old-header file would
    silently misalign columns — the header would still say N fields while
    new rows have more. Rewrite the whole file with the current header
    instead, filling any newly added columns empty for already-collected
    rows; a file whose header already matches is left untouched."""
    if not csv_path.exists():
        return
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        existing_fields = reader.fieldnames or []
        if list(existing_fields) == CSV_FIELDS:
            return
        rows = list(reader)

    print(f"Migrating {csv_path}: {len(existing_fields)} -> {len(CSV_FIELDS)} columns "
          f"({len(rows)} existing rows keep their data; new columns start empty).")
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in CSV_FIELDS})


def open_writer(csv_path: Path):
    _migrate_csv_if_needed(csv_path)
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
            except Exception as exc:
                # A page whose structure deviates from what the parsing code
                # expects (this has happened twice already — jobLocation and
                # hiringOrganization each once had an unexpected shape) should
                # cost one vacancy, not the whole run's progress.
                print(f"  [error] failed to parse {url}: {exc}")
                scraper.errors += 1
                continue
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
