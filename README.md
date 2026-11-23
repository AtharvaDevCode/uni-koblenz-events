# Koblenz Campus Events

Automated campus event aggregator and timetable tracker for students at the University of Koblenz.

**Live Feed:** https://atharvadevcode.github.io/uni-koblenz-events/

## What It Does

* **Multi-Source Aggregation:** Pulls upcoming events from the University of Koblenz Plone API, the latest AStA Newsletter archive, and dynamically discovered multi-day event program pages (such as WinterUni and International Week).
* **Structured Extraction & Translation:** Uses Groq LLMs with Pydantic schema validation and automatic model rotation to normalize dates to `Europe/Berlin` local time, translate German listings into English, tag spoken languages (`EN`, `DE`, `Neutral`), and extract registration links.
* **Deduplication:** Merges cross-posted listings across the university calendar and AStA newsletters using fuzzy title matching, campus synonym normalization, and venue-level overlap checks.
* **Four Views:**
  * **Day:** Defaults to the current date with previous/next day navigation and active multi-day series banners.
  * **Agenda:** Chronological feed grouped by date.
  * **Calendar:** Monthly grid with event indicators and single-day drill-down.
  * **Register:** Filtered list of upcoming events requiring sign-up or tickets.
* **Client-Side Search & Controls:** Intent-aware search (language, free entry, time-of-day, topic synonyms), `EN` / `DE` text toggle, local bookmarking (`Saved`), and dark/light theme support.

## Project Structure

```text
├── index.html                  # Zero-dependency frontend (HTML/CSS/JS)
├── scraper.py                  # Plone API, newsletter & program page scraper
├── events.json                 # Deduplicated upcoming events feed
├── cache.json                  # Processed item hashes and cached page keys
├── requirements.txt            # Python dependencies
└── .github/workflows/sync.yml  # Daily automated GitHub Actions sync
```

## Local Setup

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Set your Groq API key in `.env` or your environment:
   ```bash
   export GROQ_API_KEY="your_key_here"
   ```
3. Run the scraper and serve the frontend locally:
   ```bash
   python scraper.py
   python -m http.server 8000
   ```
   Open `http://localhost:8000`.

## Automated Deployment

`.github/workflows/sync.yml` runs daily at `06:30 UTC` (and manually via `workflow_dispatch`), executes `scraper.py` using the `GROQ_API_KEY` repository secret, and commits updates to `events.json` and `cache.json` directly to `main`, which GitHub Pages serves statically.