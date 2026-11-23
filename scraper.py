import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, ValidationError
from rapidfuzz import fuzz

try:
    from zoneinfo import ZoneInfo
    BERLIN_TZ = ZoneInfo("Europe/Berlin")
except Exception:
    BERLIN_TZ = None

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# --- Config ---

CACHE_VERSION = "v4"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELS = [
    "openai/gpt-oss-120b",
    "qwen/qwen3.8-27b",
    "llama-3.3-70b-versatile",
    "openai/gpt-oss-20b",
]

BATCH_SIZE = 10
BATCH_DELAY_SEC = 2.5
CHUNK_CHARS = 6500
_model_cursor = 0

ALL_UNI_EVENTS = True

EVENTS_FILE = Path("events.json")
CACHE_FILE = Path("cache.json")

API_HEADERS = {
    "Accept": "application/json",
    "Referer": "https://campus-koblenz.app/",
    "User-Agent": "KoblenzCampusTracker/1.0",
}

HTML_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.uni-koblenz.de/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}

# --- 1. Output Schema ---

class ExtractedEvent(BaseModel):
    item_idx: Optional[int] = None
    original_lang: Literal["de", "en", "mixed"] = "de"
    title_original: str
    title_en: str
    description_original: str = ""
    description_en: str = ""
    event_language: Literal["EN", "DE", "Neutral"] = "DE"
    category: Literal[
        "Social & Parties",
        "Academic & Workshops",
        "Career & Jobs",
        "Sports & Games",
        "International",
        "Politics & Admin",
    ] = "Academic & Workshops"
    start_datetime: str  # ISO 8601: YYYY-MM-DDTHH:MM:SS
    end_datetime: Optional[str] = None
    location: Optional[str] = None
    registration_required: bool = False
    registration_url: Optional[str] = None
    is_free: Optional[bool] = None
    source_url: Optional[str] = None


class ExtractionBatch(BaseModel):
    events: list[ExtractedEvent]


# --- 2. Helpers ---

DAY_HEADER_RE = re.compile(
    r"^(MONTAG|DIENSTAG|MITTWOCH|DONNERSTAG|FREITAG|SAMSTAG|SONNTAG|"
    r"MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY|"
    r"\d{1,2}\.\d{1,2}\.(?:20\d{2})?).*$",
    re.IGNORECASE,
)


def to_local_iso(ts: Optional[str]) -> Optional[str]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(BERLIN_TZ) if BERLIN_TZ else dt.astimezone()
            dt = dt.replace(tzinfo=None)
        return dt.isoformat(timespec="seconds")
    except Exception:
        return ts


def is_multi_day_range(start_iso: Optional[str], end_iso: Optional[str]) -> bool:
    if not start_iso or not end_iso or start_iso[:10] == end_iso[:10]:
        return False
    try:
        hrs = (datetime.fromisoformat(end_iso) - datetime.fromisoformat(start_iso)).total_seconds() / 3600
        return hrs >= 18
    except Exception:
        return False


def clean_program_text(text: str) -> str:
    cut = re.search(r"\nANMELDUNGEN\s*\n", text)
    if cut:
        text = text[: cut.start()]
    return text.strip()


def chunk_text_with_context(text: str, max_chars: int = CHUNK_CHARS) -> list[str]:
    chunks, current, length = [], [], 0
    active_day_header = ""

    for line in text.splitlines():
        stripped = line.strip()
        if stripped and DAY_HEADER_RE.match(stripped) and len(stripped) < 60:
            active_day_header = stripped

        if length + len(line) + 1 > max_chars and current:
            chunks.append("\n".join(current))
            current = [f"[CONTINUED - CURRENT DAY: {active_day_header}]"] if active_day_header else []
            length = sum(len(x) + 1 for x in current)

        current.append(line)
        length += len(line) + 1

    if current:
        chunks.append("\n".join(current))
    return chunks


def strip_newsletter_toc(text: str) -> str:
    match = re.search(r"Inhalt.*?Infos rund ums Campusleben", text, flags=re.DOTALL)
    if match:
        return text[match.end() - len("Infos rund ums Campusleben"):]
    return text


def load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            if data.get("version") == CACHE_VERSION:
                return data
            print(f"[Cache] Upgrading cache to {CACHE_VERSION} (clearing stale entries)...")
        except Exception:
            pass
    return {"version": CACHE_VERSION, "plone_hashes": [], "cached_pages": []}


def save_cache(cache: dict):
    cache["version"] = CACHE_VERSION
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def load_existing_upcoming_events() -> list[dict]:
    if not EVENTS_FILE.exists():
        return []
    try:
        data = json.loads(EVENTS_FILE.read_text(encoding="utf-8"))
        today = datetime.now().strftime("%Y-%m-%d")
        return [
            ev for ev in data.get("events", [])
            if ev.get("start_datetime", "")[:10] >= today
        ]
    except Exception:
        return []


def hash_plone_item(item: dict) -> str:
    raw = f"{item.get('url')}|{item.get('start')}|{item.get('title')}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# --- 3. Fetchers ---

def fetch_plone_api(client: httpx.Client) -> list[dict]:
    url = "https://campus-koblenz.app/api/de/ssv/asta/referent-innen/ramon-roeser/feed-campus-app/@querystring-search"

    query = [
        {
            "i": "portal_type",
            "o": "plone.app.querystring.operation.selection.any",
            "v": ["Event"],
        },
        {
            "i": "end",
            "o": "plone.app.querystring.operation.date.afterToday",
            "v": "",
        },
    ]
    if not ALL_UNI_EVENTS:
        query.insert(
            1,
            {
                "i": "Subject",
                "o": "plone.app.querystring.operation.selection.any",
                "v": ["AStA", "StuPa"],
            },
        )

    payload = {
        "b_size": "99",
        "b_start": 0,
        "metadata_fields": "_all",
        "query": query,
        "sort_on": "start",
        "sort_order": "ascending",
    }

    r = client.post(url, headers=API_HEADERS, json=payload)
    r.raise_for_status()
    items = r.json().get("items", [])

    cleaned = []
    for idx, item in enumerate(items):
        real_url = item.get("@id", "").replace(
            "https://campus-koblenz.app/api", "https://www.uni-koblenz.de"
        )
        cleaned.append(
            {
                "item_idx": idx,
                "title": item.get("title"),
                "description": (item.get("description") or "")[:500],
                "start": to_local_iso(item.get("start")),
                "end": to_local_iso(item.get("end")),
                "location": item.get("location"),
                "subjects": item.get("Subject", []),
                "url": real_url,
            }
        )
    return cleaned


def fetch_page_soup(client: httpx.Client, url: str) -> tuple[BeautifulSoup, httpx.URL]:
    r = client.get(url, headers=HTML_HEADERS)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser"), r.url


def extract_clean_text(soup: BeautifulSoup, base_url: httpx.URL, selector: Optional[str] = None) -> str:
    root = soup.select_one(selector) if selector else soup
    if root is None:
        root = soup
    for tag in root(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    for a in root.find_all("a", href=True):
        href = a["href"]
        if href.startswith("/"):
            href = f"{base_url.scheme}://{base_url.host}{href}"
        text = a.get_text(strip=True)
        if href.startswith("http") and href != text:
            a.replace_with(f"{text} ({href})")
        else:
            a.replace_with(text)
    return root.get_text("\n", strip=True)


def find_program_sublinks(soup: BeautifulSoup, base_url: httpx.URL, page_url: str) -> list[str]:
    root = soup.select_one("#content-core") or soup
    found = []
    for a in root.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith("/"):
            href = f"{base_url.scheme}://{base_url.host}{href}"
        if not href.startswith("http") or href == page_url:
            continue
        if any(ext in href.lower() for ext in (".ics", ".pdf", "mailto:", "instagram.com", "facebook.com")):
            continue
        label = a.get_text(" ", strip=True).lower()
        parent_txt = a.parent.get_text(" ", strip=True).lower() if a.parent else ""
        if any(k in label or k in parent_txt for k in ("programm", "program", "schedule", "externe webseite", "alle veranstaltungen")):
            if "uni-koblenz.de" in href and href not in found:
                found.append(href)
    return found[:2]


def fetch_latest_asta_newsletter(client: httpx.Client) -> tuple[str, str]:
    archive_url = "https://asta-newsletter.uni-koblenz.de/archiv"
    soup, _ = fetch_page_soup(client, archive_url)

    links = soup.select('a.summary.url[title="Newsletter"]')
    if not links:
        raise RuntimeError("No newsletter links found on archive page")

    latest_url = links[-1]["href"]
    issue_soup, issue_base = fetch_page_soup(client, latest_url)
    raw_text = extract_clean_text(issue_soup, issue_base, selector="#content-core")
    return latest_url, strip_newsletter_toc(raw_text)


# --- 4. Groq LLM Extraction with Robust Rotation & Retry-After ---

def normalize_with_groq(
    client: httpx.Client,
    api_key: str,
    source_name: str,
    fallback_url: str,
    raw_content: str,
    url_map: Optional[dict[int, str]] = None,
) -> list[dict]:
    if not raw_content or raw_content.strip() in ("[]", "{}"):
        return []

    today = datetime.now().strftime("%Y-%m-%d")

    system_prompt = (
        "You extract structured University of Koblenz student events and return ONLY valid JSON "
        'matching this exact structure: {"events": [{ '
        '"item_idx": int or null, '
        '"original_lang": "de"|"en"|"mixed", '
        '"title_original": str, '
        '"title_en": str, '
        '"description_original": str, '
        '"description_en": str, '
        '"event_language": "EN"|"DE"|"Neutral", '
        '"category": "Social & Parties"|"Academic & Workshops"|"Career & Jobs"|"Sports & Games"|"International"|"Politics & Admin", '
        '"start_datetime": "YYYY-MM-DDTHH:MM:SS", '
        '"end_datetime": "YYYY-MM-DDTHH:MM:SS" or null, '
        '"location": str or null, '
        '"registration_required": bool, '
        '"registration_url": str or null, '
        '"is_free": bool or null, '
        '"source_url": str or null '
        "}]}"
    )

    user_prompt = f"""
Today is {today}. Source: {source_name} (page URL: {fallback_url}).

Extract ALL UPCOMING student-relevant events (on or after {today}).
Skip past events, political campaign ads/manifestos (like 'Vote for list X'), and staff-only admin sessions.
Important: If a page lists a schedule of individual sub-events (workshops, tournaments like Mario Kart, bowling nights, parties, cultural stage, talks), extract EVERY SINGLE sub-event with its exact date, start/end time, and room!
Rules:
1. If input items have `item_idx`, copy that exact integer into `item_idx`.
2. Keep the original title & a concise 1-2 sentence description in `title_original` / `description_original`.
3. Provide clean English versions in `title_en` / `description_en` (if already English, copy them).
4. Set `event_language`: "EN" if held in English, "Neutral" for games/sports/parties/food/bowling/Mario Kart where German isn't required, "DE" if German.
5. Pick the single best `category` from the allowed values.
6. Format dates strictly as ISO 8601 (YYYY-MM-DDTHH:MM:SS). If no time is given, use 00:00:00.
7. Extract `registration_url` if a signup/ticket link is mentioned. If registration is on the same page, set `registration_required: true` and `registration_url` to `{fallback_url}`.

INPUT DATA:
{raw_content}
"""

    groq_headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    global _model_cursor
    start_idx = _model_cursor % len(GROQ_MODELS)
    _model_cursor += 1
    ordered_models = GROQ_MODELS[start_idx:] + GROQ_MODELS[:start_idx]

    last_error = None
    for round_num in range(4):
        max_retry_after = 12.0
        for model in ordered_models:
            try:
                r = client.post(
                    GROQ_URL,
                    headers=groq_headers,
                    json={
                        "model": model,
                        "temperature": 0.1,
                        "response_format": {"type": "json_object"},
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                    },
                    timeout=60.0,
                )

                if r.status_code in (429, 503):
                    ra = float(r.headers.get("retry-after", 10))
                    max_retry_after = min(max(max_retry_after, ra), 25.0)
                    print(f"    [{model}] Busy ({r.status_code}), rotating...")
                    continue

                if r.status_code == 400:
                    print(f"    [{model}] JSON 400 error, rotating...")
                    last_error = f"{model}: 400 Bad Request"
                    continue

                r.raise_for_status()
                content = r.json()["choices"][0]["message"]["content"]
                batch = ExtractionBatch.model_validate_json(content)

                out = []
                for ev in batch.events:
                    d = ev.model_dump()
                    idx = d.pop("item_idx", None)
                    real_url = (url_map or {}).get(idx) if idx is not None else None
                    llm_url = d.pop("source_url", None)
                    if llm_url and "campus-koblenz.app" in llm_url:
                        llm_url = None
                    final_url = real_url or llm_url or d.get("registration_url") or fallback_url

                    d["sources"] = [source_name]
                    d["source_urls"] = [final_url]
                    out.append(d)
                return out

            except (httpx.HTTPError, ValidationError, KeyError, json.JSONDecodeError) as e:
                last_error = f"{model}: {e}"
                print(f"    [{model}] Failed ({type(e).__name__}), rotating...")

        if round_num < 3:
            print(f"    All models cooling down, waiting {max_retry_after:.0f}s (round {round_num + 1}/3)...")
            time.sleep(max_retry_after)

    raise RuntimeError(f"All Groq models failed. Last error: {last_error}")


# --- 5. Smart Deduplication ---

GENERIC_LOCATIONS = {
    "", "online", "koblenz", "universität koblenz", "university of koblenz",
    "campus", "zoom", "videokonferenz", "campus koblenz",
}

STOP_WORDS = {
    "event", "events", "party", "night", "evening", "abend", "student", "students",
    "studierende", "studis", "koblenz", "asta", "stupa", "winteruni", "sommeruni",
    "international", "week", "woche", "meeting", "workshop", "lecture", "vortrag",
    "universität", "university", "campus", "with", "from", "this", "that", "your",
    "eine", "einen", "einem", "einer", "über", "unter", "nach", "beim", "zum", "zur",
    "und", "der", "die", "das", "den", "dem", "des", "für", "mit", "von", "auf",
    "program", "programm", "online", "info", "veranstaltung", "termin", "more",
}

SYNONYMS = [
    (r"\bwinter\s+uni\b", "winteruni"),
    (r"\bwintermarkt\b", "winter market"),
    (r"\bstupa\b", "student parliament"),
    (r"\bstudierendenparlament\b", "student parliament"),
    (r"\bwahlen\b", "elections"),
    (r"\bwahl\b", "elections"),
    (r"\bprogram\s+now\s+online\b", "program"),
    (r"\b202[5-9]\b", ""),
]


def _clean_text(s: str) -> str:
    t = re.sub(r"[^a-z0-9äöüß\s]", " ", (s or "").lower())
    for pat, repl in SYNONYMS:
        t = re.sub(pat, repl, t)
    return re.sub(r"\s+", " ", t).strip()


def _parse_date(iso: str):
    try:
        return datetime.fromisoformat(iso[:10]).date()
    except Exception:
        return None


def _is_multi_day_ev(ev: dict) -> bool:
    return is_multi_day_range(ev.get("start_datetime"), ev.get("end_datetime"))


def _is_midnight_ish(iso: str) -> bool:
    return iso.endswith("00:00:00") or iso.endswith("23:00:00")


def _is_same_event(a: dict, b: dict) -> bool:
    if _is_multi_day_ev(a) != _is_multi_day_ev(b):
        return False

    sa, sb = _parse_date(a.get("start_datetime", "")), _parse_date(b.get("start_datetime", ""))
    if not sa or not sb:
        return False

    day_diff = abs((sa - sb).days)
    if day_diff > 1:
        return False
    if day_diff == 1 and not (_is_midnight_ish(a["start_datetime"]) or _is_midnight_ish(b["start_datetime"])):
        return False

    t_en_a, t_en_b = _clean_text(a["title_en"]), _clean_text(b["title_en"])
    t_or_a, t_or_b = _clean_text(a["title_original"]), _clean_text(b["title_original"])

    if max(fuzz.token_sort_ratio(t_en_a, t_en_b), fuzz.token_sort_ratio(t_or_a, t_or_b)) >= 80:
        return True

    for ta, tb in ((t_en_a, t_en_b), (t_or_a, t_or_b)):
        if ta and tb and fuzz.token_set_ratio(ta, tb) >= 88:
            shorter, longer = sorted((len(ta), len(tb)))
            if shorter / longer >= 0.45:
                return True

    if day_diff == 0:
        words_a = {w for w in (t_en_a + " " + t_or_a).split() if len(w) > 3 and w not in STOP_WORDS}
        words_b = {w for w in (t_en_b + " " + t_or_b).split() if len(w) > 3 and w not in STOP_WORDS}
        if len(words_a & words_b) >= 2:
            return True

        loc_a, loc_b = _clean_text(a.get("location") or ""), _clean_text(b.get("location") or "")
        if loc_a not in GENERIC_LOCATIONS and loc_b not in GENERIC_LOCATIONS:
            if fuzz.token_set_ratio(loc_a, loc_b) >= 85 and (words_a & words_b):
                return True

    return False


def deduplicate(events: list[dict]) -> list[dict]:
    today = datetime.now().strftime("%Y-%m-%d")
    merged: list[dict] = []

    for ev in events:
        ev_date = ev.get("start_datetime", "")[:10]
        if ev_date < today:
            continue

        title_l = (ev.get("title_en", "") + " " + ev.get("title_original", "")).lower()
        if any(k in title_l for k in ("schmetterlinx", "schängelcher", "die liste -")):
            continue

        ev["source_urls"] = [
            u for u in ev.get("source_urls", [])
            if u and "campus-koblenz.app" not in u
        ]
        if not ev["source_urls"] and ev.get("registration_url"):
            ev["source_urls"] = [ev["registration_url"]]

        match = next((ex for ex in merged if _is_same_event(ex, ev)), None)

        if match:
            for s in ev.get("sources", []):
                if s not in match["sources"]:
                    match["sources"].append(s)
            for u in ev.get("source_urls", []):
                if u not in match["source_urls"]:
                    match["source_urls"].append(u)
            if not match.get("registration_url") and ev.get("registration_url"):
                match["registration_url"] = ev["registration_url"]
            if ev.get("registration_required"):
                match["registration_required"] = True
            if match.get("is_free") is None and ev.get("is_free") is not None:
                match["is_free"] = ev["is_free"]
            if not match.get("location") and ev.get("location"):
                match["location"] = ev["location"]
            if _is_midnight_ish(match["start_datetime"]) and not _is_midnight_ish(ev["start_datetime"]):
                match["start_datetime"] = ev["start_datetime"]
                match["end_datetime"] = ev.get("end_datetime") or match.get("end_datetime")
            if len(ev.get("description_en", "")) > len(match.get("description_en", "")):
                match["description_en"] = ev["description_en"]
                match["description_original"] = ev["description_original"]
        else:
            key = f"{ev_date}-{_clean_text(ev['title_en'])}"
            ev["id"] = hashlib.sha256(key.encode()).hexdigest()[:12]
            merged.append(ev)

    return sorted(merged, key=lambda x: x["start_datetime"])


# --- 6. Chunked Page Runner ---

def process_chunked_page(
    client: httpx.Client,
    api_key: str,
    source_name: str,
    page_url: str,
    raw_text: str,
    cache_key: str,
    cached_pages: set[str],
    all_events: list[dict],
    force_pages: bool = False,
):
    if not force_pages and cache_key in cached_pages:
        print(f"[{source_name}] Already cached: {page_url}")
        return

    cleaned = clean_program_text(raw_text)
    chunks = chunk_text_with_context(cleaned, CHUNK_CHARS)
    print(f"[{source_name}] Processing {page_url} in {len(chunks)} chunk(s)...")
    failed = 0
    for idx, chunk in enumerate(chunks, 1):
        print(f"  -> Chunk {idx}/{len(chunks)} ({len(chunk)} chars)...")
        try:
            extracted = normalize_with_groq(client, api_key, source_name, page_url, chunk)
            print(f"     Extracted {len(extracted)} events")
            all_events.extend(extracted)
        except Exception as e:
            failed += 1
            print(f"     [WARN] Chunk {idx} failed: {e}")
        if idx < len(chunks):
            time.sleep(BATCH_DELAY_SEC)

    if failed == 0:
        cached_pages.add(cache_key)
    else:
        print(f"  [WARN] {failed} chunk(s) failed — not caching {source_name} so it retries next run.")


# --- 7. Main Pipeline ---

def main():
    force_pages = "--force-pages" in sys.argv

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        print("[ERROR] GROQ_API_KEY is not set.")
        return

    cache = load_cache()
    seen_hashes = set(cache.get("plone_hashes", []))
    cached_pages = set() if force_pages else set(cache.get("cached_pages", []))
    all_events = load_existing_upcoming_events()
    if all_events:
        print(f"[Cache] Loaded {len(all_events)} previously scraped upcoming events")

    multi_day_pages: list[tuple[str, str]] = []

    with httpx.Client(timeout=25.0, follow_redirects=True) as client:
        # 1. Uni Plone JSON API
        try:
            plone_items = fetch_plone_api(client)
            url_map = {it["item_idx"]: it["url"] for it in plone_items}

            for it in plone_items:
                if is_multi_day_range(it.get("start"), it.get("end")) and it.get("url"):
                    title = it.get("title") or "Multi-day Event"
                    if "uni-koblenz.de" in it["url"]:
                        multi_day_pages.append((title, it["url"]))

            new_items = [it for it in plone_items if hash_plone_item(it) not in seen_hashes]
            print(f"[Plone API] {len(plone_items)} total items ({len(new_items)} new to process)")

            for i in range(0, len(new_items), BATCH_SIZE):
                chunk = new_items[i : i + BATCH_SIZE]
                batch_num = i // BATCH_SIZE + 1
                total_batches = (len(new_items) + BATCH_SIZE - 1) // BATCH_SIZE
                print(f"  -> Batch {batch_num}/{total_batches} ({len(chunk)} items)...")
                try:
                    extracted = normalize_with_groq(
                        client,
                        api_key,
                        "Uni Koblenz Calendar",
                        "https://www.uni-koblenz.de",
                        json.dumps(chunk, ensure_ascii=False),
                        url_map=url_map,
                    )
                    print(f"     Extracted {len(extracted)} student events")
                    all_events.extend(extracted)
                    for it in chunk:
                        seen_hashes.add(hash_plone_item(it))
                except Exception as e:
                    print(f"     [WARN] Batch {batch_num} failed: {e}")

                if i + BATCH_SIZE < len(new_items):
                    time.sleep(BATCH_DELAY_SEC)
        except Exception as e:
            print(f"[WARN] Plone API fetch failed: {e}")

        # 2. Deep-scrape Multi-Day Event Pages
        seen_page_urls = set()
        for title, page_url in multi_day_pages:
            if page_url in seen_page_urls:
                continue
            seen_page_urls.add(page_url)
            try:
                soup, base_url = fetch_page_soup(client, page_url)
                sublinks = find_program_sublinks(soup, base_url, page_url)
                page_text = extract_clean_text(soup, base_url, selector="#content-core")
                if len(page_text) > 350:
                    process_chunked_page(
                        client, api_key, title, page_url, page_text,
                        f"{CACHE_VERSION}:{page_url}", cached_pages, all_events,
                        force_pages=force_pages,
                    )
                for sub_url in sublinks:
                    if sub_url in seen_page_urls:
                        continue
                    seen_page_urls.add(sub_url)
                    try:
                        sub_soup, sub_base = fetch_page_soup(client, sub_url)
                        sub_text = extract_clean_text(sub_soup, sub_base, selector="#content-core")
                        if len(sub_text) > 350:
                            process_chunked_page(
                                client, api_key, f"{title} Program", sub_url, sub_text,
                                f"{CACHE_VERSION}:{sub_url}", cached_pages, all_events,
                                force_pages=force_pages,
                            )
                    except Exception as e:
                        print(f"[WARN] Subpage {sub_url} failed: {e}")
            except Exception as e:
                print(f"[WARN] Multi-day page {page_url} failed: {e}")

        # 3. Latest AStA Newsletter
        try:
            nl_url, nl_text = fetch_latest_asta_newsletter(client)
            process_chunked_page(
                client, api_key, "AStA Newsletter", nl_url, nl_text,
                f"{CACHE_VERSION}:{nl_url}", cached_pages, all_events,
                force_pages=force_pages,
            )
        except Exception as e:
            print(f"[WARN] AStA Newsletter failed: {e}")

    final_events = deduplicate(all_events)
    output = {
        "last_updated": datetime.now().isoformat(timespec="seconds"),
        "count": len(final_events),
        "events": final_events,
    }

    EVENTS_FILE.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    cache["plone_hashes"] = sorted(seen_hashes)
    cache["cached_pages"] = sorted(cached_pages)
    save_cache(cache)

    print(f"\nDone! Saved {len(final_events)} deduplicated events to {EVENTS_FILE}")


if __name__ == "__main__":
    main()