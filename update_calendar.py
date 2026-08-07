#!/usr/bin/env python3
"""Build a rich, auto-updating iCalendar feed from Explore Edmonton events."""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, time as dt_time, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

from curl_cffi import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from icalendar import Calendar, Event

BASE_URL = "https://exploreedmonton.com"
CALENDAR_URL = f"{BASE_URL}/event-calendar"
OUTPUT = Path("docs/explore-edmonton-events.ics")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
TIMEOUT = 30
MAX_LISTING_PAGES = 30
REQUEST_DELAY = 0.25
EDMONTON_TZ = ZoneInfo("America/Edmonton")
GENERATOR_VERSION = "schedule-aware-v3.1"

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
MONTH_PATTERN = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?"
)
WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

session = requests.Session(impersonate="chrome")
session.headers.update({
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-CA,en-US;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
})

_browser_state: dict[str, Any] = {}

def _browser_html(url: str) -> str:
    """Fetch a fully rendered page with Chromium. Used when anti-bot blocks HTTP clients."""
    if "playwright" not in _browser_state:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="en-CA",
            timezone_id="America/Edmonton",
            viewport={"width": 1440, "height": 1000},
            extra_http_headers={
                "Accept-Language": "en-CA,en-US;q=0.9,en;q=0.8",
                "DNT": "1",
            },
        )
        _browser_state.update(playwright=pw, browser=browser, context=context)
    page = _browser_state["context"].new_page()
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)
        # Scroll once to trigger lazy-loaded event cards/content.
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1200)
        status = response.status if response else 0
        html = page.content()
        lowered = html.lower()
        if status >= 400 or "403 forbidden" in lowered or "access denied" in lowered:
            raise RuntimeError(f"Chromium received blocked response ({status}) for {url}")
        return html
    finally:
        page.close()


def fetch_html(url: str) -> str:
    """Fetch HTML with Chrome TLS impersonation; fall back to real Chromium on blocking."""
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = session.get(url, timeout=TIMEOUT, allow_redirects=True)
            if response.status_code == 200 and response.text.strip():
                return response.text
            last_error = RuntimeError(f"HTTP {response.status_code}")
        except Exception as exc:
            last_error = exc
        if attempt < 2:
            time.sleep(2 ** attempt)
    print(f"HTTP client blocked for {url} ({last_error}); trying Chromium...", file=sys.stderr)
    return _browser_html(url)


def close_browser() -> None:
    if not _browser_state:
        return
    for key in ("context", "browser"):
        obj = _browser_state.get(key)
        if obj:
            try:
                obj.close()
            except Exception:
                pass
    pw = _browser_state.get("playwright")
    if pw:
        try:
            pw.stop()
        except Exception:
            pass
    _browser_state.clear()


@dataclass(frozen=True)
class ParsedEvent:
    title: str
    start: date | datetime
    end: date | datetime | None
    location: str
    latitude: float | None
    longitude: float | None
    plain_description: str
    html_description: str
    url: str
    image_url: str
    organizer: str
    category: str
    price: str
    ticket_url: str
    schedule_text: str
    updated: datetime


def event_links_from_html(html: str, page_url: str) -> set[str]:
    soup = BeautifulSoup(html, "html.parser")
    links: set[str] = set()
    for anchor in soup.select('a[href*="/event-calendar/"]'):
        href = anchor.get("href")
        if not href:
            continue
        url = urljoin(page_url, href).split("#", 1)[0].split("?", 1)[0]
        parsed = urlparse(url)
        if parsed.netloc.endswith("exploreedmonton.com") and parsed.path.rstrip("/") != "/event-calendar":
            links.add(url.rstrip("/"))
    return links


def discover_event_links() -> list[str]:
    links: set[str] = set()
    seen_pages: set[str] = set()
    pending = [CALENDAR_URL]
    while pending and len(seen_pages) < MAX_LISTING_PAGES:
        page_url = pending.pop(0)
        if page_url in seen_pages:
            continue
        seen_pages.add(page_url)
        html = fetch_html(page_url)
        links.update(event_links_from_html(html, page_url))
        soup = BeautifulSoup(html, "html.parser")
        for anchor in soup.select("a[href]"):
            text = anchor.get_text(" ", strip=True).lower()
            rel = " ".join(anchor.get("rel", [])) if anchor.get("rel") else ""
            href = anchor.get("href", "")
            if text in {"load more", "next", "next page"} or "next" in rel:
                next_url = urljoin(page_url, href)
                if next_url.startswith(CALENDAR_URL) and next_url not in seen_pages:
                    pending.append(next_url)
        if page_url == CALENDAR_URL:
            for page in range(2, MAX_LISTING_PAGES + 1):
                pending.append(f"{CALENDAR_URL}?page={page}")
        time.sleep(REQUEST_DELAY)
    return sorted(links)


def iter_jsonld(soup: BeautifulSoup) -> Iterable[dict[str, Any]]:
    for node in soup.select('script[type="application/ld+json"]'):
        raw = node.string or node.get_text()
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                yield item
                graph = item.get("@graph")
                if isinstance(graph, list):
                    stack.extend(graph)
            elif isinstance(item, list):
                stack.extend(item)


def is_event_schema(item: dict[str, Any]) -> bool:
    event_type = item.get("@type")
    if isinstance(event_type, list):
        return any(str(t).lower().endswith("event") for t in event_type)
    return str(event_type).lower().endswith("event")


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def parse_temporal(value: Any) -> date | datetime | None:
    if not value:
        return None
    try:
        parsed = dateparser.parse(str(value))
    except (ValueError, TypeError, OverflowError):
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value).strip()):
        return parsed.date()
    return parsed


def location_details(location: Any) -> tuple[str, float | None, float | None]:
    if isinstance(location, str):
        return clean_text(location), None, None
    if not isinstance(location, dict):
        return "", None, None
    parts: list[str] = []
    if location.get("name"):
        parts.append(clean_text(location["name"]))
    address = location.get("address")
    if isinstance(address, str):
        parts.append(clean_text(address))
    elif isinstance(address, dict):
        address_parts = [clean_text(address.get(k)) for k in (
            "streetAddress", "addressLocality", "addressRegion", "postalCode", "addressCountry"
        ) if address.get(k)]
        if address_parts:
            parts.append(", ".join(address_parts))
    geo = location.get("geo") if isinstance(location.get("geo"), dict) else {}
    try:
        lat = float(geo.get("latitude")) if geo.get("latitude") is not None else None
        lon = float(geo.get("longitude")) if geo.get("longitude") is not None else None
    except (TypeError, ValueError):
        lat = lon = None
    return ", ".join(dict.fromkeys(p for p in parts if p)), lat, lon


def person_or_org_name(value: Any) -> str:
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, dict):
        return clean_text(value.get("name"))
    if isinstance(value, list):
        return ", ".join(filter(None, (person_or_org_name(v) for v in value)))
    return ""


def image_url(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("url") or value.get("contentUrl") or "")
    if isinstance(value, list) and value:
        return image_url(value[0])
    return ""


def offer_details(value: Any) -> tuple[str, str]:
    offers = value if isinstance(value, list) else [value] if isinstance(value, dict) else []
    prices: list[str] = []
    ticket = ""
    for offer in offers:
        if not isinstance(offer, dict):
            continue
        if not ticket and offer.get("url"):
            ticket = str(offer["url"])
        price = offer.get("price")
        currency = offer.get("priceCurrency")
        if price not in (None, ""):
            prices.append(f"{currency + ' ' if currency else ''}{price}")
        elif offer.get("description"):
            prices.append(clean_text(offer["description"]))
    return ", ".join(dict.fromkeys(prices)), ticket


def main_root(soup: BeautifulSoup):
    return soup.select_one("main") or soup.select_one("article") or soup.body or soup


def page_text(soup: BeautifulSoup) -> str:
    return clean_text(main_root(soup).get_text(" ", strip=True))


def date_banner_candidates(soup: BeautifulSoup) -> list[str]:
    """Find compact visible strings that look like the event's displayed schedule."""
    root = main_root(soup)
    found: list[tuple[int, str]] = []
    seen: set[str] = set()
    month_re = re.compile(rf"\b(?:{MONTH_PATTERN})\b", re.I)
    day_re = re.compile(r"\b(?:[1-9]|[12]\d|3[01])\b")
    for node in root.find_all(["h1", "h2", "h3", "h4", "time", "div", "span", "p"]):
        text = clean_text(node.get_text(" ", strip=True))
        if not text or len(text) > 110 or text in seen:
            continue
        if not month_re.search(text) or not day_re.search(text):
            continue
        seen.add(text)
        score = 0
        if node.name in {"h1", "h2", "h3", "time"}:
            score += 6
        if "," in text or "&" in text:
            score += 4
        if text.upper() == text:
            score += 2
        score -= len(text) // 40
        found.append((score, text))
    found.sort(key=lambda x: (-x[0], len(x[1])))
    return [text for _, text in found[:25]]


def _month_num(token: str) -> int | None:
    return MONTHS.get(token.lower().rstrip("."))


def parse_explicit_date_list(text: str, reference_year: int) -> list[date]:
    """Parse non-contiguous dates like 'JULY 29, AUGUST 5, 12, & 19'."""
    normalized = re.sub(r"\s+", " ", text.replace("\u2013", "-").replace("\u2014", "-")).strip()
    # Don't expand ordinary displayed ranges such as AUG 3 - AUG 8.
    range_re = re.compile(
        rf"\b(?:{MONTH_PATTERN})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?\s*"
        rf"(?:-|\bto\b)\s*(?:(?:{MONTH_PATTERN})\.?\s+)?\d{{1,2}}",
        re.I,
    )
    if range_re.search(normalized):
        return []
    month_matches = list(re.finditer(rf"\b({MONTH_PATTERN})\.?\b", normalized, re.I))
    if not month_matches:
        return []
    dates: list[date] = []
    current_year = reference_year
    previous_month: int | None = None
    for index, match in enumerate(month_matches):
        month = _month_num(match.group(1))
        if month is None:
            continue
        if previous_month is not None and month < previous_month and previous_month >= 11 and month <= 2:
            current_year += 1
        previous_month = month
        segment_end = month_matches[index + 1].start() if index + 1 < len(month_matches) else len(normalized)
        segment = normalized[match.end():segment_end]
        days = [int(d) for d in re.findall(r"\b([0-3]?\d)(?:st|nd|rd|th)?\b", segment, re.I)]
        for day in days:
            if not 1 <= day <= 31:
                continue
            try:
                dates.append(date(current_year, month, day))
            except ValueError:
                pass
    unique = list(dict.fromkeys(dates))
    return unique if len(unique) >= 2 else []


def find_explicit_dates(soup: BeautifulSoup, reference_year: int) -> tuple[list[date], str]:
    for candidate in date_banner_candidates(soup):
        dates = parse_explicit_date_list(candidate, reference_year)
        if dates:
            return dates, candidate
    return [], ""


def parse_clock_token(raw: str) -> dt_time | None:
    token = raw.lower().replace(".", "")
    token = re.sub(r"\s+", "", token)
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm)", token)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if not (1 <= hour <= 12 and 0 <= minute <= 59):
        return None
    if hour == 12:
        hour = 0
    if match.group(3) == "pm":
        hour += 12
    return dt_time(hour, minute)


def find_time_window(text: str) -> tuple[dt_time | None, dt_time | None, str]:
    token = r"\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)"
    for pattern in (
        rf"\bfrom\s+({token})\s+(?:to|until|through|-)\s+({token})",
        rf"\b({token})\s*(?:to|until|through|-)\s*({token})",
    ):
        match = re.search(pattern, text, re.I)
        if match:
            start = parse_clock_token(match.group(1))
            end = parse_clock_token(match.group(2))
            if start and end:
                return start, end, match.group(0)
    return None, None, ""


def weekly_dates_from_copy(text: str, start: date, end: date, reference_year: int) -> list[date]:
    years = {int(y) for y in re.findall(r"\b(20\d{2})\b", text)}
    if years and reference_year not in years:
        return []
    match = re.search(
        r"\b(?:every|each)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?\b",
        text, re.I,
    )
    if not match:
        return []
    weekday = WEEKDAYS[match.group(1).lower()]
    cursor = start
    while cursor <= end and cursor.weekday() != weekday:
        cursor += timedelta(days=1)
    dates: list[date] = []
    while cursor <= end:
        dates.append(cursor)
        cursor += timedelta(days=7)
    return dates if len(dates) >= 2 else []


def combine_local(day: date, clock: dt_time) -> datetime:
    return datetime.combine(day, clock, tzinfo=EDMONTON_TZ)


def normalize_occurrences(
    soup: BeautifulSoup,
    baseline_start: date | datetime,
    baseline_end: date | datetime | None,
) -> tuple[list[tuple[date | datetime, date | datetime | None]], str]:
    text = page_text(soup)
    reference_year = baseline_start.year
    clock_start, clock_end, _ = find_time_window(text)
    dates, _ = find_explicit_dates(soup, reference_year)

    if not dates and isinstance(baseline_start, date) and not isinstance(baseline_start, datetime):
        start_day = baseline_start
        end_day = baseline_end if isinstance(baseline_end, date) and not isinstance(baseline_end, datetime) else start_day
        dates = weekly_dates_from_copy(text, start_day, end_day, reference_year)

    if dates:
        occurrences: list[tuple[date | datetime, date | datetime | None]] = []
        for day in dates:
            if clock_start:
                start_value = combine_local(day, clock_start)
                end_value = combine_local(day, clock_end) if clock_end else None
                if end_value is not None and end_value <= start_value:
                    end_value += timedelta(days=1)
            else:
                start_value = day
                end_value = day
            occurrences.append((start_value, end_value))
        date_part = ", ".join(d.strftime("%b %d, %Y").replace(" 0", " ") for d in dates)
        if clock_start and clock_end:
            def fmt(t: dt_time) -> str:
                x = datetime.combine(date.today(), t).strftime("%I:%M %p").lstrip("0")
                return x.replace(":00 ", " ")
            return occurrences, f"{date_part} | {fmt(clock_start)}-{fmt(clock_end)}"
        return occurrences, date_part

    # Upgrade a single all-day date to a timed event when the body has a clear time range.
    if (
        clock_start
        and isinstance(baseline_start, date) and not isinstance(baseline_start, datetime)
        and (baseline_end is None or baseline_end == baseline_start)
    ):
        start_value = combine_local(baseline_start, clock_start)
        end_value = combine_local(baseline_start, clock_end) if clock_end else None
        if end_value is not None and end_value <= start_value:
            end_value += timedelta(days=1)
        return [(start_value, end_value)], ""

    return [(baseline_start, baseline_end)], ""


def event_body_paragraphs(soup: BeautifulSoup, title: str, location: str) -> list[str]:
    """Get useful event-copy paragraphs while filtering labels, cards, cookies and site chrome."""
    root = main_root(soup)
    paragraphs: list[str] = []
    seen: set[str] = set()
    bad_prefixes = (
        "time street address", "event information", "by navigating this website",
        "we use cookies", "choose from edmonton", "sign up", "subscribe",
    )
    for node in root.find_all(["p", "li"]):
        text = clean_text(node.get_text(" ", strip=True))
        if not text or len(text) < 25 or len(text) > 1800:
            continue
        low = text.lower()
        if low.startswith(bad_prefixes) or "time street address" in low:
            continue
        if title and text.casefold() == title.casefold():
            continue
        if location and text.casefold() == location.casefold():
            continue
        if text not in seen:
            seen.add(text)
            paragraphs.append(text)
    return paragraphs[:12]


def external_cta_url(soup: BeautifulSoup, event_url: str) -> str:
    root = main_root(soup)
    for anchor in root.select("a[href]"):
        label = clean_text(anchor.get_text(" ", strip=True)).lower()
        if label not in {"visit site", "tickets", "buy tickets", "register", "learn more"}:
            continue
        href = urljoin(event_url, str(anchor.get("href", "")))
        if href and "exploreedmonton.com" not in urlparse(href).netloc:
            return href
    return ""


def build_descriptions(item: dict[str, Any], soup: BeautifulSoup, url: str, location: str,
                       organizer: str, category: str, price: str, ticket_url: str,
                       schedule_text: str, title: str) -> tuple[str, str]:
    teaser = clean_text(item.get("description"))
    paragraphs = event_body_paragraphs(soup, title, location)
    content: list[str] = []
    if teaser:
        content.append(teaser)
    for paragraph in paragraphs:
        if teaser and (paragraph in teaser or teaser in paragraph):
            if len(paragraph) > len(teaser):
                content[0] = paragraph
            continue
        content.append(paragraph)
    content = list(dict.fromkeys(content))

    plain = content[:]
    html = [f"<p>{escape(p)}</p>" for p in content]
    facts: list[tuple[str, str]] = []
    if schedule_text:
        facts.append(("Schedule", schedule_text))
    if location:
        facts.append(("Location", location))
    if organizer:
        facts.append(("Organizer", organizer))
    if category:
        facts.append(("Category", category))
    if price:
        facts.append(("Price", price))
    if ticket_url:
        facts.append(("Visit / Tickets", ticket_url))
    facts.append(("Explore Edmonton", url))
    plain.append("EVENT INFORMATION\n" + "\n".join(f"{k}: {v}" for k, v in facts))
    html.append("<h3>Event Information</h3><ul>" + "".join(
        f"<li><strong>{escape(k)}:</strong> {escape(v)}</li>" for k, v in facts
    ) + "</ul>")
    return "\n\n".join(plain).strip(), "".join(html)

def parse_event(url: str) -> list[ParsedEvent]:
    html = fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")
    candidates = [item for item in iter_jsonld(soup) if is_event_schema(item)]
    if not candidates:
        return []
    item = candidates[0]
    title = clean_text(item.get("name"))
    start = parse_temporal(item.get("startDate"))
    end = parse_temporal(item.get("endDate"))
    if not title or start is None:
        return []

    location, latitude, longitude = location_details(item.get("location"))
    canonical = soup.select_one('link[rel="canonical"]')
    canonical_url = urljoin(url, str(canonical.get("href") if canonical else item.get("url") or url))
    organizer = person_or_org_name(item.get("organizer") or item.get("performer"))
    category = clean_text(item.get("eventType") or item.get("keywords"))
    price, ticket_url = offer_details(item.get("offers"))
    if not ticket_url:
        ticket_url = external_cta_url(soup, canonical_url)
    img = image_url(item.get("image"))

    occurrences, schedule_text = normalize_occurrences(soup, start, end)
    plain_desc, html_desc = build_descriptions(
        item, soup, canonical_url, location, organizer, category, price, ticket_url,
        schedule_text, title
    )
    updated = datetime.now(timezone.utc)
    base = ParsedEvent(
        title, start, end, location, latitude, longitude, plain_desc, html_desc,
        canonical_url, img, organizer, category, price, ticket_url, schedule_text, updated
    )
    return [replace(base, start=occ_start, end=occ_end) for occ_start, occ_end in occurrences]

def uid_for(event: ParsedEvent) -> str:
    raw = f"{event.url}|{event.start.isoformat()}|{event.title}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32] + "@explore-edmonton-calendar"


def add_event(cal: Calendar, parsed: ParsedEvent) -> None:
    event = Event()
    event.add("uid", uid_for(parsed))
    event.add("summary", parsed.title)
    event.add("dtstart", parsed.start)
    if parsed.end is not None:
        end = parsed.end
        if isinstance(parsed.start, date) and not isinstance(parsed.start, datetime) and isinstance(end, date) and not isinstance(end, datetime):
            end += timedelta(days=1)
        event.add("dtend", end)
    elif isinstance(parsed.start, date) and not isinstance(parsed.start, datetime):
        event.add("dtend", parsed.start + timedelta(days=1))

    if parsed.location:
        event.add("location", parsed.location)
    if parsed.latitude is not None and parsed.longitude is not None:
        event.add("geo", (parsed.latitude, parsed.longitude))
    if parsed.plain_description:
        event.add("description", parsed.plain_description)
    if parsed.html_description:
        event.add("x-alt-desc", parsed.html_description, parameters={"FMTTYPE": "text/html"})
    event.add("url", parsed.url)
    if parsed.ticket_url:
        event.add("x-tickets-url", parsed.ticket_url)
    if parsed.organizer:
        event.add("x-organizer-name", parsed.organizer)
    if parsed.category:
        event.add("categories", [c.strip() for c in re.split(r"[,;]", parsed.category) if c.strip()])
    if parsed.price:
        event.add("x-price", parsed.price)
    if parsed.image_url:
        event.add("attach", parsed.image_url, parameters={"FMTTYPE": "image/jpeg"})
        event.add("x-image", parsed.image_url)
    event.add("last-modified", parsed.updated.astimezone(timezone.utc))
    event.add("dtstamp", datetime.now(timezone.utc))
    event.add("status", "CONFIRMED")
    event.add("transp", "TRANSPARENT")
    cal.add_component(event)


def build_calendar(events: list[ParsedEvent]) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//Jhosep Noa//Explore Edmonton Events Rich Feed//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", "Explore Edmonton Events")
    cal.add("x-wr-timezone", "America/Edmonton")
    cal.add("x-generator-version", GENERATOR_VERSION)
    cal.add("x-published-ttl", "PT6H")
    cal.add("refresh-interval", "PT6H", parameters={"VALUE": "DURATION"})
    for parsed in sorted(events, key=lambda e: e.start.isoformat()):
        add_event(cal, parsed)
    return cal.to_ical()


def main() -> int:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    print(f"Explore Edmonton calendar generator: {GENERATOR_VERSION}")
    try:
        links = discover_event_links()
        print(f"Discovered {len(links)} event pages")
        events: list[ParsedEvent] = []
        failures = 0
        for index, url in enumerate(links, 1):
            try:
                parsed_events = parse_event(url)
                if parsed_events:
                    events.extend(parsed_events)
                    if len(parsed_events) > 1:
                        print(f"Expanded {url} into {len(parsed_events)} occurrences")
                else:
                    failures += 1
                    print(f"WARN no Event JSON-LD: {url}", file=sys.stderr)
            except Exception as exc:
                failures += 1
                print(f"WARN failed {url}: {exc}", file=sys.stderr)
            if index % 20 == 0:
                print(f"Processed {index}/{len(links)}")
            time.sleep(REQUEST_DELAY)
        if not events:
            raise RuntimeError("No events were parsed; existing calendar was left unchanged")
        OUTPUT.write_bytes(build_calendar(events))
        print(f"Wrote {len(events)} events to {OUTPUT} ({failures} skipped)")
        return 0
    finally:
        close_browser()


if __name__ == "__main__":
    raise SystemExit(main())
