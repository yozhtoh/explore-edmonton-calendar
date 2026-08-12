#!/usr/bin/env python3
"""Build a rich, auto-updating iCalendar feed from Explore Edmonton events."""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

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


def visible_sections(soup: BeautifulSoup) -> list[tuple[str, str]]:
    """Extract useful event-page headings and their nearby copy while excluding site chrome."""
    root = soup.select_one("main") or soup.select_one("article") or soup.body
    if not root:
        return []
    sections: list[tuple[str, str]] = []
    banned = {"book hotel now", "choose from edmonton's best hotels", "related events", "share"}
    for heading in root.select("h2, h3, h4"):
        title = clean_text(heading.get_text(" ", strip=True))
        if not title or title.lower() in banned or len(title) > 120:
            continue
        chunks: list[str] = []
        for sibling in heading.next_siblings:
            name = getattr(sibling, "name", None)
            if name in {"h2", "h3", "h4"}:
                break
            if name in {"script", "style", "nav", "footer", "form"}:
                continue
            text = clean_text(getattr(sibling, "get_text", lambda *a, **k: str(sibling))(" ", strip=True))
            if text and text.lower() not in banned:
                chunks.append(text)
            if sum(map(len, chunks)) > 1800:
                break
        body = "\n".join(dict.fromkeys(chunks)).strip()
        if body:
            sections.append((title, body))
    return sections[:12]


def build_descriptions(item: dict[str, Any], soup: BeautifulSoup, url: str, location: str,
                       organizer: str, category: str, price: str, ticket_url: str) -> tuple[str, str]:
    summary = clean_text(item.get("description"))
    sections = visible_sections(soup)

    plain: list[str] = []
    html: list[str] = []
    if summary:
        plain.append(summary)
        html.append(f"<p>{escape(summary)}</p>")
    for heading, body in sections:
        if body in summary or heading.lower() in {"event details", "details"}:
            continue
        plain.append(f"{heading.upper()}\n{body}")
        html.append(f"<h3>{escape(heading)}</h3><p>{escape(body).replace(chr(10), '<br>')}</p>")

    facts = []
    if location:
        facts.append(("Location", location))
    if organizer:
        facts.append(("Organizer", organizer))
    if category:
        facts.append(("Category", category))
    if price:
        facts.append(("Price", price))
    if ticket_url:
        facts.append(("Tickets", ticket_url))
    facts.append(("Event page", url))
    if facts:
        plain.append("EVENT INFORMATION\n" + "\n".join(f"{k}: {v}" for k, v in facts))
        html.append("<h3>Event Information</h3><ul>" + "".join(
            f"<li><strong>{escape(k)}:</strong> {escape(v)}</li>" for k, v in facts
        ) + "</ul>")
    return "\n\n".join(plain).strip(), "".join(html)


def parse_event(url: str) -> ParsedEvent | None:
    html = fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")
    candidates = [item for item in iter_jsonld(soup) if is_event_schema(item)]
    if not candidates:
        return None
    item = candidates[0]
    title = clean_text(item.get("name"))
    start = parse_temporal(item.get("startDate"))
    end = parse_temporal(item.get("endDate"))
    if not title or start is None:
        return None

    location, latitude, longitude = location_details(item.get("location"))
    canonical = soup.select_one('link[rel="canonical"]')
    canonical_url = urljoin(url, str(canonical.get("href") if canonical else item.get("url") or url))
    organizer = person_or_org_name(item.get("organizer") or item.get("performer"))
    category = clean_text(item.get("eventType") or item.get("keywords"))
    price, ticket_url = offer_details(item.get("offers"))
    img = image_url(item.get("image"))
    plain_desc, html_desc = build_descriptions(
        item, soup, canonical_url, location, organizer, category, price, ticket_url
    )

    updated = datetime.now(timezone.utc)

    return ParsedEvent(title, start, end, location, latitude, longitude, plain_desc, html_desc,
                       canonical_url, img, organizer, category, price, ticket_url, updated)


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
    cal.add("x-published-ttl", "PT6H")
    cal.add("refresh-interval", "PT6H", parameters={"VALUE": "DURATION"})
    for parsed in sorted(events, key=lambda e: e.start.isoformat()):
        add_event(cal, parsed)
    return cal.to_ical()


def main() -> int:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    try:
        links = discover_event_links()
        print(f"Discovered {len(links)} event pages")
        events: list[ParsedEvent] = []
        failures = 0
        for index, url in enumerate(links, 1):
            try:
                parsed = parse_event(url)
                if parsed:
                    events.append(parsed)
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
