# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the App

```bash
python3 app.py
```

Starts the Flask dev server on `http://0.0.0.0:5000`. No build step required.

**Dependencies** are not pinned in any manifest. Ensure these are installed:
```bash
pip install flask requests
```

## Architecture

This is a two-file Flask app: `app.py` (backend, ~450 lines) and `index.html` (single-page frontend, ~1000 lines with embedded CSS and JS).

### Persistent State (JSON files in `~`)

- `~/.rzd_config.json` — user settings: SMTP, routes list, monitoring interval, enabled flag
- `~/.rzd_state.json` — dedup state: tracks which `{route_id}:{date}:{train_number}:{bucket}` keys have already triggered emails
- `~/.rzd_history.json` — ring buffer of last 2600 monitoring check results

### Background Monitoring Thread

`monitor_loop()` runs as a daemon thread. It iterates all active routes and their dates, calls the РЖД API (`fetch_trains()`), and emails when new trains appear. Start/stop is controlled via `stop_event` (`threading.Event`). The API endpoints `/api/monitoring` toggle this thread at runtime.

### Seat Bucketing for Dedup

To avoid re-sending notifications on minor seat count fluctuations, seats are grouped into 10-seat buckets: `bucket = (total_seats // 10) * 10`. A notification fires when a new bucket key appears in the state. This means a train with 5 seats won't re-alert when it goes to 8, but will alert again if it reaches 10.

### РЖД API

`fetch_trains()` calls `https://ticket.rzd.ru/api/v1/railway-service/prices/train-pricing`. The session uses a spoofed Chrome User-Agent and Referer header — required by the РЖД endpoint. Station codes are numeric strings resolved via the autocomplete endpoint `/api/station_search` (backed by a suggest API).

### REST API Endpoints

| Endpoint | Methods | Purpose |
|---|---|---|
| `/api/config` | GET, POST | Read/write SMTP settings and interval |
| `/api/routes` | GET, POST, PUT, DELETE | CRUD for monitored routes |
| `/api/monitoring` | GET, POST | Start/stop background thread |
| `/api/check_now` | POST | One-shot manual check |
| `/api/history` | GET, DELETE | Read/clear history log |
| `/api/test_email` | POST | SMTP connectivity test |
| `/api/station_search` | GET | Proxy to РЖД station autocomplete |

### Frontend

`index.html` is a self-contained SPA with no build tooling (no npm, webpack, etc.). It uses Lucide icons (CDN) and vanilla JS. Navigation has four views: **Routes**, **Check** (manual search), **History**, **Settings**. Station search uses a 300ms debounce and caches station codes in a JS object keyed by display name.

### Email

`send_email()` uses `smtplib` with STARTTLS (default port 587). Each route can override the global recipient list via its own `email_to` field; if absent, falls back to the global config value. Multiple recipients are split on commas or semicolons.