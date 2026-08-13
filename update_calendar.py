#!/usr/bin/env python3
"""Generate a timed-only iCalendar feed from Explore Edmonton event pages.

Design rules:
- Only publish events that have an explicit clock time on the event page or in Event JSON-LD.
- Never publish VALUE=DATE / all-day events.
- Never invent a start time or duration.
- If a page lists several explicit dates with one shared time range, create one VEVENT per date.
- If a date range is paired with an explicit recurrence (for example "every Saturday"), expand it.
- If a multi-day date range has no unambiguous timed recurrence, skip it rather than guess.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time as time_module
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Tag
from dateutil import parser as dateparser
from playwright.sync_api import sync_playwright

BASE_URL = "https://exploreedmonton.com"
CALENDAR_URL = f"{BASE_URL}/event-calendar"
OUTPUT = Path("docs/explore-edmonton-events.ics")
TZ_NAME = "America/Edmonton"
TZ = ZoneInfo(TZ_NAME)
MAX_LISTING_PAGES = 30
PAGE_WAIT_MS = 1200
REQUEST_DELAY_SECONDS = 0.12
BUILD_ID = "2026-08-12-timed-only-clean-v1"

MONTHS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}
MONTH_PATTERN = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
WEEKDAYS = {
    "monday": 0, "mondays": 0,
    "tuesday": 1, "tuesdays": 1,
    "wednesday": 2, "wednesdays": 2,
    "thursday": 3, "thursdays": 3,
    "friday": 4, "fridays": 4,
    "saturday": 5, "saturdays": 5,
    "sunday": 6, "sundays": 6,
}

# Require either AM/PM or a colon so ordinary numbers such as street addresses are not times.
TIME_TOKEN_PATTERN = (
    r"(?:"
    r"(?:1[0-2]|0?[1-9])(?::[0-5]\d)?\s*(?:a\.?\s*m\.?|p\.?\s*m\.?)"
    r"|"
    r"(?:[01]?\d|2[0-3]):[0-5]\d"
    r")"
)
TIME_TOKEN_RE = re.compile(TIME_TOKEN_PATTERN, re.IGNORECASE)
TIME_RANGE_RE = re.compile(
    rf"(?P<start>{TIME_TOKEN_PATTERN})\s*(?:-|–|—|to|until|through)\s*(?P<end>{TIME_TOKEN_PATTERN})",
    re.IGNORECASE,
)
DATE_RANGE_RE = re.compile(
    rf"(?P<m1>{MONTH_PATTERN})\s+(?P<d1>[0-3]?\d)(?:st|nd|rd|th)?\s*(?:-|–|—|to|through)\s*"
    rf"(?:(?P<m2>{MONTH_PATTERN})\s+)?(?P<d2>[0-3]?\d)(?:st|nd|rd|th)?(?:,?\s*(?P<year>20\d{{2}}))?",
    re.IGNORECASE,
)
DATE_TOKEN_RE = re.compile(
    rf"(?P<month>{MONTH_PATTERN})|(?P<day>\b(?:[12]?\d|3[01])(?:st|nd|rd|th)?\b)|(?P<year>\b20\d{{2}}\b)",
    re.IGNORECASE,
)

BOILERPLATE_PHRASES = (
    "book hotel now",
    "choose from edmonton's best hotels",
    "we use cookies",
    "cookies policy",
    "located in treaty 6 territory",
    "follow explore edmonton on social",
    "visitor experience roadmap",
    "privacy policy",
    "terms of use",
    "join our site",
)

PROMO_HEADINGS = {
    "related events",
    "book hotel now",
    "choose from edmonton's best hotels",
    "what's on",
    "more info",
    "share",
}


@dataclass(frozen=True)
class Occurrence:
    start: datetime
    end: datetime | None
    schedule_text: str


@dataclass(frozen=True)
class EventMeta:
    title: str
    url: str
    location: str
    latitude: float | None
    longitude: float | None
    image_url: str
    organizer: str
    category: str
    price: str
    ticket_url: str
    description_plain: str
    description_html: str
    updated: datetime


class BrowserFetcher:
    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    def __enter__(self) -> "BrowserFetcher":
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        self._context = self._browser.new_context(
            locale="en-CA",
            timezone_id=TZ_NAME,
            viewport={"width": 1440, "height": 1000},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/127.0.0.0 Safari/537.36"
            ),
        )
        self._page = self._context.new_page()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        for obj in (self._page, self._context, self._browser):
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass

    def html(self, url: str) -> str:
        assert self._page is not None
        response = self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
        self._page.wait_for_timeout(PAGE_WAIT_MS)
        self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        self._page.wait_for_timeout(500)
        status = response.status if response else 0
        html = self._page.content()
        lowered = html.lower()
        if status >= 400 or "403 forbidden" in lowered or "access denied" in lowered:
            raise RuntimeError(f"blocked response {status} for {url}")
        return html


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def normalized_lines(text: str) -> list[str]:
    return [re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()]


def iter_jsonld(soup: BeautifulSoup) -> Iterable[dict[str, Any]]:
    for node in soup.select('script[type="application/ld+json"]'):
        raw = node.string or node.get_text()
        if not raw or not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        stack: list[Any] = data if isinstance(data, list) else [data]
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                yield value
                graph = value.get("@graph")
                if isinstance(graph, list):
                    stack.extend(graph)
            elif isinstance(value, list):
                stack.extend(value)


def is_event_schema(item: dict[str, Any]) -> bool:
    value = item.get("@type")
    values = value if isinstance(value, list) else [value]
    return any(str(v).lower().endswith("event") for v in values if v)


def find_event_schema(soup: BeautifulSoup) -> dict[str, Any]:
    candidates = [item for item in iter_jsonld(soup) if is_event_schema(item)]
    if not candidates:
        return {}
    # Prefer the candidate with the most event-specific fields.
    return max(candidates, key=lambda x: sum(bool(x.get(k)) for k in ("name", "startDate", "location", "description", "image")))


def discover_event_links(fetcher: BrowserFetcher) -> list[str]:
    links: set[str] = set()
    empty_pages = 0
    for page_number in range(1, MAX_LISTING_PAGES + 1):
        page_url = CALENDAR_URL if page_number == 1 else f"{CALENDAR_URL}?page={page_number}"
        html = fetcher.html(page_url)
        soup = BeautifulSoup(html, "html.parser")
        before = len(links)
        for anchor in soup.select('a[href*="/event-calendar/"]'):
            href = anchor.get("href")
            if not href:
                continue
            url = urljoin(page_url, href).split("#", 1)[0].split("?", 1)[0].rstrip("/")
            parsed = urlparse(url)
            if parsed.netloc.endswith("exploreedmonton.com") and parsed.path.rstrip("/") != "/event-calendar":
                links.add(url)
        if len(links) == before:
            empty_pages += 1
        else:
            empty_pages = 0
        # Most pagination implementations repeat or stop once there is no next page.
        if page_number >= 2 and empty_pages >= 2:
            break
        time_module.sleep(REQUEST_DELAY_SECONDS)
    return sorted(links)


def parse_structured_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    raw = str(value).strip()
    # Date-only structured values are deliberately rejected here.
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return None
    try:
        parsed = dateparser.parse(raw)
    except (ValueError, TypeError, OverflowError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.astimezone(TZ)


def structured_year(item: dict[str, Any], page_text: str) -> int:
    for key in ("startDate", "endDate"):
        value = item.get(key)
        if value:
            match = re.search(r"\b(20\d{2})\b", str(value))
            if match:
                return int(match.group(1))
    match = re.search(r"\b(20\d{2})\b", page_text)
    if match:
        return int(match.group(1))
    return datetime.now(TZ).year


def parse_clock(raw: str) -> time:
    compact = raw.lower().replace(".", "").replace(" ", "")
    ampm_match = re.fullmatch(r"(1[0-2]|0?[1-9])(?::([0-5]\d))?(am|pm)", compact)
    if ampm_match:
        hour = int(ampm_match.group(1))
        minute = int(ampm_match.group(2) or "0")
        ampm = ampm_match.group(3)
        if hour == 12:
            hour = 0
        if ampm == "pm":
            hour += 12
        return time(hour, minute)
    twenty_four = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", compact)
    if twenty_four:
        return time(int(twenty_four.group(1)), int(twenty_four.group(2)))
    raise ValueError(f"unsupported clock value: {raw}")


def unique_time_tokens(text: str) -> list[str]:
    seen: list[str] = []
    for match in TIME_TOKEN_RE.finditer(text):
        token = re.sub(r"\s+", " ", match.group(0)).strip()
        normalized = token.lower().replace(".", "").replace(" ", "")
        if normalized not in {t.lower().replace(".", "").replace(" ", "") for t in seen}:
            seen.append(token)
    return seen


def date_range_from_text(text: str, fallback_year: int) -> tuple[date, date] | None:
    match = DATE_RANGE_RE.search(text)
    if not match:
        return None
    m1 = MONTHS[match.group("m1").lower()]
    m2 = MONTHS[(match.group("m2") or match.group("m1")).lower()]
    year = int(match.group("year") or fallback_year)
    try:
        start = date(year, m1, int(match.group("d1")))
        end = date(year, m2, int(match.group("d2")))
    except ValueError:
        return None
    if end < start and m2 < m1:
        end = date(year + 1, m2, int(match.group("d2")))
    return start, end


def explicit_dates_from_text(text: str, fallback_year: int) -> list[date]:
    """Parse explicit month/day lists such as 'July 29, August 5, 12, & 19'."""
    # Remove clock tokens so 11:00 / 6:00 cannot be misread as calendar days.
    scrubbed = TIME_TOKEN_RE.sub(" ", text)
    year_match = re.search(r"\b(20\d{2})\b", scrubbed)
    year = int(year_match.group(1)) if year_match else fallback_year
    current_month: int | None = None
    result: list[date] = []
    for match in DATE_TOKEN_RE.finditer(scrubbed):
        if match.group("month"):
            current_month = MONTHS[match.group("month").lower()]
            continue
        if match.group("year"):
            continue
        if match.group("day") and current_month is not None:
            day_value = int(re.sub(r"(?:st|nd|rd|th)$", "", match.group("day"), flags=re.IGNORECASE))
            try:
                candidate = date(year, current_month, day_value)
            except ValueError:
                continue
            if candidate not in result:
                result.append(candidate)
    return result


def expand_range_with_recurrence(start: date, end: date, context: str) -> list[date]:
    if start == end:
        return [start]
    lower = context.lower()
    if re.search(r"\b(?:daily|every day|each day)\b", lower):
        days: list[date] = []
        current = start
        while current <= end:
            days.append(current)
            current += timedelta(days=1)
        return days
    for word, weekday in WEEKDAYS.items():
        if re.search(rf"\bevery\s+{re.escape(word)}\b|\b{re.escape(word)}\b", lower):
            days = []
            current = start
            while current <= end:
                if current.weekday() == weekday:
                    days.append(current)
                current += timedelta(days=1)
            return days
    return []


def date_context_for_block(blocks: list[str], index: int) -> str:
    # Keep the timed block first, then nearby lines that may contain the page's date heading.
    chosen = [blocks[index]]
    for distance in range(1, 6):
        for candidate_index in (index - distance, index + distance):
            if 0 <= candidate_index < len(blocks):
                candidate = blocks[candidate_index]
                if re.search(MONTH_PATTERN, candidate, re.IGNORECASE):
                    chosen.append(candidate)
    return " | ".join(chosen)


def occurrences_from_visible_text(blocks: list[str], fallback_year: int) -> list[Occurrence]:
    occurrences: list[Occurrence] = []

    for index, block in enumerate(blocks):
        if not TIME_TOKEN_RE.search(block):
            continue
        context = date_context_for_block(blocks, index)

        range_matches = list(TIME_RANGE_RE.finditer(block))
        if range_matches:
            for time_match in range_matches:
                start_clock = parse_clock(time_match.group("start"))
                end_clock = parse_clock(time_match.group("end"))

                explicit_dates = explicit_dates_from_text(context, fallback_year)
                date_range = date_range_from_text(context, fallback_year)

                # A range heading such as Aug 21-Aug 23 is not an explicit list. Avoid
                # treating its two endpoints as two sessions unless recurrence text is present.
                if date_range and len(explicit_dates) <= 2:
                    dates = expand_range_with_recurrence(date_range[0], date_range[1], context)
                else:
                    dates = explicit_dates

                for event_date in dates:
                    start_dt = datetime.combine(event_date, start_clock, TZ)
                    end_date = event_date
                    if end_clock <= start_clock:
                        end_date += timedelta(days=1)
                    end_dt = datetime.combine(end_date, end_clock, TZ)
                    occurrences.append(Occurrence(start_dt, end_dt, clean_text(block)))
            continue

        # If there is no range, accept one and only one explicit clock time. No duration is invented.
        tokens = unique_time_tokens(block)
        if len(tokens) != 1:
            continue
        start_clock = parse_clock(tokens[0])
        explicit_dates = explicit_dates_from_text(context, fallback_year)
        date_range = date_range_from_text(context, fallback_year)
        if date_range and len(explicit_dates) <= 2:
            dates = expand_range_with_recurrence(date_range[0], date_range[1], context)
        else:
            dates = explicit_dates
        for event_date in dates:
            start_dt = datetime.combine(event_date, start_clock, TZ)
            occurrences.append(Occurrence(start_dt, None, clean_text(block)))

    # Deduplicate repeated schedule text from responsive/mobile DOM copies.
    deduped: dict[tuple[str, str | None], Occurrence] = {}
    for occurrence in occurrences:
        key = (
            occurrence.start.isoformat(),
            occurrence.end.isoformat() if occurrence.end else None,
        )
        deduped[key] = occurrence
    return sorted(deduped.values(), key=lambda o: o.start)


def location_details(location: Any) -> tuple[str, float | None, float | None]:
    if isinstance(location, str):
        return clean_text(location), None, None
    if not isinstance(location, dict):
        return "", None, None

    parts: list[str] = []
    name = clean_text(location.get("name"))
    if name:
        parts.append(name)
    address = location.get("address")
    if isinstance(address, str):
        parts.append(clean_text(address))
    elif isinstance(address, dict):
        address_parts = [
            clean_text(address.get(key))
            for key in ("streetAddress", "addressLocality", "addressRegion", "postalCode", "addressCountry")
            if address.get(key)
        ]
        if address_parts:
            parts.append(", ".join(address_parts))

    geo = location.get("geo") if isinstance(location.get("geo"), dict) else {}
    try:
        latitude = float(geo.get("latitude")) if geo.get("latitude") is not None else None
        longitude = float(geo.get("longitude")) if geo.get("longitude") is not None else None
    except (TypeError, ValueError):
        latitude = longitude = None

    return ", ".join(dict.fromkeys(p for p in parts if p)), latitude, longitude


def fallback_geo_from_dom(soup: BeautifulSoup) -> tuple[float | None, float | None]:
    for element in soup.find_all(attrs={"data-latitude": True, "data-longitude": True}):
        try:
            return float(element["data-latitude"]), float(element["data-longitude"])
        except (KeyError, TypeError, ValueError):
            pass
    html = str(soup)
    match = re.search(r'"latitude"\s*:\s*"?(-?\d+(?:\.\d+)?)"?.{0,160}?"longitude"\s*:\s*"?(-?\d+(?:\.\d+)?)"?', html, re.I | re.S)
    if match:
        try:
            return float(match.group(1)), float(match.group(2))
        except ValueError:
            pass
    return None, None


def person_name(value: Any) -> str:
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, dict):
        return clean_text(value.get("name"))
    if isinstance(value, list):
        return ", ".join(filter(None, (person_name(item) for item in value)))
    return ""


def image_url(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("url") or value.get("contentUrl") or "")
    if isinstance(value, list):
        for item in value:
            candidate = image_url(item)
            if candidate:
                return candidate
    return ""


def offer_details(value: Any) -> tuple[str, str]:
    values = value if isinstance(value, list) else [value] if isinstance(value, dict) else []
    prices: list[str] = []
    ticket_url = ""
    for offer in values:
        if not isinstance(offer, dict):
            continue
        if not ticket_url and offer.get("url"):
            ticket_url = str(offer["url"])
        if offer.get("price") not in (None, ""):
            currency = clean_text(offer.get("priceCurrency"))
            price = clean_text(offer.get("price"))
            prices.append(f"{currency} {price}".strip())
        elif offer.get("description"):
            prices.append(clean_text(offer.get("description")))
    return ", ".join(dict.fromkeys(prices)), ticket_url


def find_event_root(soup: BeautifulSoup, title: str) -> Tag:
    main = soup.select_one("main") or soup.body or soup
    heading = soup.find("h1")
    if not isinstance(heading, Tag):
        return main  # type: ignore[return-value]

    best: Tag | None = None
    node: Tag | None = heading
    while node is not None:
        text = clean_text(node.get_text(" ", strip=True))
        long_paragraphs = [clean_text(p.get_text(" ", strip=True)) for p in node.find_all("p")]
        if title.lower() in text.lower() and any(len(p) >= 80 for p in long_paragraphs) and len(text) <= 14000:
            best = node
            break
        parent = node.parent
        node = parent if isinstance(parent, Tag) else None
        if node is main:
            break
    return best or main  # type: ignore[return-value]


def event_text_blocks(root: Tag) -> list[str]:
    blocks: list[str] = []
    selectors = "h1,h2,h3,h4,p,li,time,[class*=date],[class*=time]"
    for element in root.select(selectors):
        text = clean_text(element.get_text(" ", strip=True))
        if not text or len(text) > 4000:
            continue
        lower = text.lower()
        if any(phrase in lower for phrase in BOILERPLATE_PHRASES):
            continue
        if text not in blocks:
            blocks.append(text)
    if not blocks:
        blocks = normalized_lines(root.get_text("\n", strip=True))
    return blocks


def is_boilerplate_paragraph(text: str) -> bool:
    lower = text.lower()
    if len(text) < 35:
        return True
    return any(phrase in lower for phrase in BOILERPLATE_PHRASES)


def description_content(root: Tag, item: dict[str, Any], title: str) -> tuple[list[tuple[str, str]], list[str]]:
    sections: list[tuple[str, str]] = []
    paragraphs: list[str] = []

    short_description = clean_text(item.get("description"))
    if short_description and not is_boilerplate_paragraph(short_description):
        paragraphs.append(short_description)

    for paragraph in root.find_all("p"):
        text = clean_text(paragraph.get_text(" ", strip=True))
        if is_boilerplate_paragraph(text):
            continue
        if text.lower() == title.lower():
            continue
        if text not in paragraphs:
            paragraphs.append(text)
        if sum(len(p) for p in paragraphs) > 7000:
            break

    for heading in root.find_all(["h2", "h3", "h4"]):
        heading_text = clean_text(heading.get_text(" ", strip=True))
        if not heading_text or heading_text.lower() in PROMO_HEADINGS or heading_text.lower() == title.lower():
            continue
        if DATE_RANGE_RE.search(heading_text) or (re.search(MONTH_PATTERN, heading_text, re.I) and re.search(r"\d", heading_text)):
            continue
        body_parts: list[str] = []
        for sibling in heading.next_siblings:
            if isinstance(sibling, Tag) and sibling.name in {"h2", "h3", "h4"}:
                break
            if isinstance(sibling, Tag) and sibling.name == "p":
                text = clean_text(sibling.get_text(" ", strip=True))
                if not is_boilerplate_paragraph(text):
                    body_parts.append(text)
            if sum(len(x) for x in body_parts) > 1800:
                break
        body = "\n".join(dict.fromkeys(body_parts))
        if body:
            sections.append((heading_text, body))
        if len(sections) >= 8:
            break

    return sections, paragraphs[:10]


def external_action_url(root: Tag, canonical_url: str) -> str:
    preferred = re.compile(r"\b(?:visit site|get tickets|tickets|buy tickets|register|registration)\b", re.I)
    for anchor in root.find_all("a", href=True):
        text = clean_text(anchor.get_text(" ", strip=True))
        href = urljoin(canonical_url, str(anchor.get("href")))
        parsed = urlparse(href)
        if preferred.search(text) and parsed.scheme in {"http", "https"}:
            return href
    return ""


def build_descriptions(
    root: Tag,
    item: dict[str, Any],
    title: str,
    url: str,
    location: str,
    organizer: str,
    category: str,
    price: str,
    ticket_url: str,
    occurrence: Occurrence,
) -> tuple[str, str]:
    sections, paragraphs = description_content(root, item, title)

    plain_parts: list[str] = []
    html_parts: list[str] = []

    for paragraph in paragraphs:
        plain_parts.append(paragraph)
        html_parts.append(f"<p>{escape(paragraph)}</p>")

    for heading, body in sections:
        if body in paragraphs:
            continue
        plain_parts.append(f"{heading.upper()}\n{body}")
        html_parts.append(f"<h3>{escape(heading)}</h3><p>{escape(body).replace(chr(10), '<br>')}</p>")

    schedule_value = occurrence.start.strftime("%A, %B %-d, %Y at %-I:%M %p")
    if occurrence.end:
        if occurrence.end.date() == occurrence.start.date():
            schedule_value += occurrence.end.strftime("–%-I:%M %p")
        else:
            schedule_value += " – " + occurrence.end.strftime("%A, %B %-d, %Y at %-I:%M %p")

    facts: list[tuple[str, str]] = [("Schedule", schedule_value)]
    if location:
        facts.append(("Location", location))
    if organizer:
        facts.append(("Organizer", organizer))
    if category:
        facts.append(("Category", category))
    if price:
        facts.append(("Price", price))
    if ticket_url:
        facts.append(("Tickets / Visit site", ticket_url))
    facts.append(("Explore Edmonton", url))

    plain_parts.append("EVENT INFORMATION\n" + "\n".join(f"{key}: {value}" for key, value in facts))
    html_parts.append(
        "<h3>Event Information</h3><ul>"
        + "".join(f"<li><strong>{escape(key)}:</strong> {escape(value)}</li>" for key, value in facts)
        + "</ul>"
    )

    return "\n\n".join(dict.fromkeys(part for part in plain_parts if part)).strip(), "".join(html_parts)


def parse_event_page(html: str, requested_url: str) -> list[tuple[EventMeta, Occurrence]]:
    soup = BeautifulSoup(html, "html.parser")
    item = find_event_schema(soup)

    h1 = soup.find("h1")
    title = clean_text(item.get("name")) or clean_text(h1.get_text(" ", strip=True) if h1 else "")
    if not title:
        return []

    canonical = soup.select_one('link[rel="canonical"]')
    canonical_href = canonical.get("href") if canonical else None
    canonical_url = urljoin(requested_url, str(canonical_href or item.get("url") or requested_url))

    root = find_event_root(soup, title)
    blocks = event_text_blocks(root)
    # If the narrow root does not expose any time, fall back to main content for schedule detection only.
    if not any(TIME_TOKEN_RE.search(block) for block in blocks):
        main = soup.select_one("main") or soup.body
        if isinstance(main, Tag) and main is not root:
            blocks = event_text_blocks(main)

    full_text = "\n".join(blocks)
    fallback_year = structured_year(item, full_text)

    # First preference: true timed Event JSON-LD.
    structured_start = parse_structured_datetime(item.get("startDate"))
    occurrences: list[Occurrence] = []
    if structured_start:
        structured_end = parse_structured_datetime(item.get("endDate"))
        if structured_end and structured_end < structured_start:
            structured_end = None
        occurrences = [Occurrence(structured_start, structured_end, "Structured event time")]
    else:
        occurrences = occurrences_from_visible_text(blocks, fallback_year)

    # Strict rule: no explicit time = no event.
    if not occurrences:
        return []

    location, latitude, longitude = location_details(item.get("location"))
    if latitude is None or longitude is None:
        dom_lat, dom_lon = fallback_geo_from_dom(soup)
        latitude = latitude if latitude is not None else dom_lat
        longitude = longitude if longitude is not None else dom_lon

    organizer = person_name(item.get("organizer") or item.get("performer"))
    category = clean_text(item.get("eventType") or item.get("keywords"))
    price, offers_url = offer_details(item.get("offers"))
    ticket_url = offers_url or external_action_url(root, canonical_url)
    image = image_url(item.get("image"))
    if not image:
        og_image = soup.select_one('meta[property="og:image"]')
        if og_image and og_image.get("content"):
            image = str(og_image.get("content"))

    updated = datetime.now(timezone.utc)
    results: list[tuple[EventMeta, Occurrence]] = []
    for occurrence in occurrences:
        plain, html_description = build_descriptions(
            root, item, title, canonical_url, location, organizer, category, price, ticket_url, occurrence
        )
        meta = EventMeta(
            title=title,
            url=canonical_url,
            location=location,
            latitude=latitude,
            longitude=longitude,
            image_url=image,
            organizer=organizer,
            category=category,
            price=price,
            ticket_url=ticket_url,
            description_plain=plain,
            description_html=html_description,
            updated=updated,
        )
        results.append((meta, occurrence))
    return results


def uid_for(meta: EventMeta, occurrence: Occurrence) -> str:
    raw = f"{meta.url}|{occurrence.start.isoformat()}|{meta.title}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32] + "@explore-edmonton-calendar"


def add_calendar_event(cal: Any, meta: EventMeta, occurrence: Occurrence) -> None:
    from icalendar import Event
    if not isinstance(occurrence.start, datetime):
        raise TypeError("all-day/date-only DTSTART is forbidden")
    if occurrence.end is not None and not isinstance(occurrence.end, datetime):
        raise TypeError("all-day/date-only DTEND is forbidden")

    event = Event()
    event.add("uid", uid_for(meta, occurrence))
    event.add("summary", meta.title)
    event.add("dtstart", occurrence.start)
    if occurrence.end is not None:
        event.add("dtend", occurrence.end)
    event.add("dtstamp", datetime.now(timezone.utc))
    event.add("last-modified", meta.updated)
    event.add("status", "CONFIRMED")
    event.add("transp", "TRANSPARENT")
    event["X-MICROSOFT-CDO-ALLDAYEVENT"] = "FALSE"

    if meta.location:
        event.add("location", meta.location)
    if meta.latitude is not None and meta.longitude is not None:
        event.add("geo", (meta.latitude, meta.longitude))
        structured_params = {"VALUE": "URI"}
        if meta.location:
            structured_params["X-TITLE"] = meta.location
        event.add(
            "x-apple-structured-location",
            f"geo:{meta.latitude},{meta.longitude}",
            parameters=structured_params,
        )

    if meta.description_plain:
        event.add("description", meta.description_plain)
    if meta.description_html:
        event.add("x-alt-desc", meta.description_html, parameters={"FMTTYPE": "text/html"})
    event.add("url", meta.url)
    if meta.ticket_url:
        event["X-TICKETS-URL"] = meta.ticket_url
    if meta.organizer:
        event["X-ORGANIZER-NAME"] = meta.organizer
    if meta.category:
        categories = [part.strip() for part in re.split(r"[,;]", meta.category) if part.strip()]
        if categories:
            event.add("categories", categories)
    if meta.price:
        event["X-PRICE"] = meta.price
    if meta.image_url:
        # URL attachment is widely ignored but harmless; X-IMAGE gives supporting clients another option.
        event.add("attach", meta.image_url)
        event["X-IMAGE"] = meta.image_url

    cal.add_component(event)


def build_calendar(parsed: list[tuple[EventMeta, Occurrence]]) -> bytes:
    from icalendar import Calendar
    cal = Calendar()
    cal.add("prodid", "-//Explore Edmonton Timed Events//Auto Calendar//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", "Explore Edmonton Timed Events")
    cal.add("x-wr-timezone", TZ_NAME)
    cal.add("x-published-ttl", "PT6H")
    cal.add("refresh-interval", "PT6H", parameters={"VALUE": "DURATION"})
    cal["X-BUILD-ID"] = BUILD_ID

    for meta, occurrence in sorted(parsed, key=lambda pair: pair[1].start):
        add_calendar_event(cal, meta, occurrence)

    payload = cal.to_ical()
    # Hard publishing gate: even a future regression cannot publish an all-day event.
    if b"DTSTART;VALUE=DATE" in payload or b"DTEND;VALUE=DATE" in payload:
        raise RuntimeError("safety check failed: generated calendar contains an all-day event")
    return payload


def self_test() -> None:
    cpkc_blocks = [
        "CPKC Women's Open Golf Days in the ICE District",
        "July 29, August 5, 12, & 19",
        "Head to the ICE District Plaza every Wednesday from 11:00 a.m. to 6:00 p.m. for a series of free, family-friendly golf events. The events take place on July 29 and August 5, 12, and 19.",
        "ICE District - 10360 102 St, Edmonton, Alberta",
    ]
    occurrences = occurrences_from_visible_text(cpkc_blocks, 2026)
    expected = [
        datetime(2026, 7, 29, 11, 0, tzinfo=TZ),
        datetime(2026, 8, 5, 11, 0, tzinfo=TZ),
        datetime(2026, 8, 12, 11, 0, tzinfo=TZ),
        datetime(2026, 8, 19, 11, 0, tzinfo=TZ),
    ]
    starts = [item.start for item in occurrences]
    assert starts == expected, (starts, expected)
    assert all(item.end and item.end.hour == 18 for item in occurrences)

    all_day_blocks = [
        "Festival Example",
        "August 21 – August 23, 2026",
        "Three days of music, food, and family fun.",
    ]
    assert occurrences_from_visible_text(all_day_blocks, 2026) == []

    ambiguous_range = [
        "Festival Example",
        "August 21 – August 23, 2026",
        "Open from 10:00 a.m. to 8:00 p.m.",
    ]
    # A broad multi-day range with no daily/every-day wording is deliberately skipped.
    assert occurrences_from_visible_text(ambiguous_range, 2026) == []

    daily_range = [
        "Festival Example",
        "August 21 – August 23, 2026",
        "Open every day from 10:00 a.m. to 8:00 p.m.",
    ]
    daily = occurrences_from_visible_text(daily_range, 2026)
    assert len(daily) == 3
    assert [item.start.date() for item in daily] == [date(2026, 8, 21), date(2026, 8, 22), date(2026, 8, 23)]

    print(f"Self-test passed ({BUILD_ID})")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return 0

    print(f"Explore Edmonton calendar builder {BUILD_ID}")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    parsed_events: list[tuple[EventMeta, Occurrence]] = []
    skipped_no_time = 0
    failures = 0

    with BrowserFetcher() as fetcher:
        links = discover_event_links(fetcher)
        print(f"Discovered {len(links)} event pages")
        for index, url in enumerate(links, 1):
            try:
                html = fetcher.html(url)
                parsed = parse_event_page(html, url)
                if parsed:
                    parsed_events.extend(parsed)
                else:
                    skipped_no_time += 1
            except Exception as exc:
                failures += 1
                print(f"WARN failed {url}: {exc}", file=sys.stderr)
            if index % 20 == 0 or index == len(links):
                print(f"Processed {index}/{len(links)} | timed occurrences={len(parsed_events)} | skipped no/ambiguous time={skipped_no_time} | failures={failures}")
            time_module.sleep(REQUEST_DELAY_SECONDS)

    if not parsed_events:
        raise RuntimeError("No timed events were found. Existing calendar was left unchanged.")

    payload = build_calendar(parsed_events)
    OUTPUT.write_bytes(payload)
    print(f"Wrote {len(parsed_events)} timed VEVENT entries to {OUTPUT}")
    print("Safety check: 0 all-day DTSTART/DTEND entries")
    print(f"Skipped {skipped_no_time} event pages with no explicit or unambiguous time; {failures} pages failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
