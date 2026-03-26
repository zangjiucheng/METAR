import json
import os
import threading
import time
from collections import deque
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .avwx_client import AvwxError, fetch_metar_data, fetch_station_data, search_station_data
from .metar_decoder import parse_metar_sections

HOST = os.getenv("METAR_HOST", "0.0.0.0")
PORT = int(os.getenv("METAR_PORT", "8000"))
AUTO_REFRESH_SECONDS = 60
SEARCH_RATE_LIMIT = (30, 60)
LIVE_RATE_LIMIT = (30, 300)
MANUAL_RATE_LIMIT = (60, 60)
SEARCH_CACHE_TTL = 300
STATION_CACHE_TTL = 3600
METAR_CACHE_TTL = 45
MAX_CACHE_ENTRIES = 256


class FixedWindowRateLimiter:
    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
        now = time.time()
        cutoff = now - window_seconds
        with self._lock:
            bucket = self._events.setdefault(key, deque())
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                retry_after = max(1, int(bucket[0] + window_seconds - now))
                return False, retry_after
            bucket.append(now)
            return True, 0


class TTLCache:
    def __init__(self, ttl_seconds: int, max_entries: int = MAX_CACHE_ENTRIES) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._data: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> object | None:
        now = time.time()
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            expires_at, value = item
            if expires_at < now:
                self._data.pop(key, None)
                return None
            return value

    def set(self, key: str, value: object) -> None:
        now = time.time()
        with self._lock:
            if len(self._data) >= self.max_entries:
                oldest_key = next(iter(self._data))
                self._data.pop(oldest_key, None)
            self._data[key] = (now + self.ttl_seconds, value)


RATE_LIMITER = FixedWindowRateLimiter()
SEARCH_CACHE = TTLCache(SEARCH_CACHE_TTL)
STATION_CACHE = TTLCache(STATION_CACHE_TTL)
METAR_CACHE = TTLCache(METAR_CACHE_TTL)


def client_ip(handler: BaseHTTPRequestHandler) -> str:
    forwarded = handler.headers.get("X-Forwarded-For", "").strip()
    if forwarded:
        return forwarded.split(",")[0].strip()
    client = handler.client_address[0] if handler.client_address else ""
    return client or "unknown"


def limit_key(handler: BaseHTTPRequestHandler, action: str) -> str:
    return f"{action}:{client_ip(handler)}"


def check_rate_limit(handler: BaseHTTPRequestHandler, action: str) -> tuple[bool, int]:
    if action == "search":
        limit, window = SEARCH_RATE_LIMIT
    elif action == "live":
        limit, window = LIVE_RATE_LIMIT
    else:
        limit, window = MANUAL_RATE_LIMIT
    return RATE_LIMITER.allow(limit_key(handler, action), limit, window)


def public_error_message(exc: Exception) -> str:
    if isinstance(exc, AvwxError):
        text = str(exc)
        lowered = text.lower()
        if "missing avwx api token" in lowered:
            return "Server API token is not configured."
        if "http 429" in lowered:
            return "Upstream weather service rate limit reached. Please try again shortly."
        if "http 403" in lowered or "http 401" in lowered:
            return "Upstream weather service rejected this request."
        if "timed out" in lowered or "urlopen error timed out" in lowered:
            return "Upstream weather service timed out. Please try again."
        return "Weather lookup is temporarily unavailable."
    return "Request failed. Please try again."


def cached_search_station_data(query: str) -> list[dict]:
    cache_key = query.strip().lower()
    cached = SEARCH_CACHE.get(cache_key)
    if isinstance(cached, list):
        return cached
    results = search_station_data(query)
    SEARCH_CACHE.set(cache_key, results)
    return results


def cached_station_data(icao: str) -> dict:
    cache_key = icao.strip().upper()
    cached = STATION_CACHE.get(cache_key)
    if isinstance(cached, dict):
        return cached
    result = fetch_station_data(icao)
    STATION_CACHE.set(cache_key, result)
    return result


def cached_metar_data(icao: str) -> dict:
    cache_key = icao.strip().upper()
    cached = METAR_CACHE.get(cache_key)
    if isinstance(cached, dict):
        return cached
    result = fetch_metar_data(icao)
    METAR_CACHE.set(cache_key, result)
    return result


def render_blocks_html(sections: list[dict[str, str]]) -> str:
    blocks = []
    for section in sections:
        title = escape(section["title"])
        token = escape(section["token"])
        description = escape(section["description"])
        layout = section.get("layout", "")
        class_name = "data-block data-block-full" if layout == "full" else "data-block"
        items = section.get("items", [])
        items_html = ""
        if items:
            rendered_items = []
            for item in items:
                item_token = escape(item.get("token", ""))
                item_description = escape(item.get("description", ""))
                rendered_items.append(
                    f"""
      <li class="remark-item">
        <div class="remark-token">{item_token}</div>
        <div class="remark-text">{item_description}</div>
      </li>"""
                )
            items_html = f"""
      <ul class="remark-list">
        {''.join(rendered_items)}
      </ul>"""
        blocks.append(
            f"""
    <section class="{class_name}">
      <div class="block-title">{title}</div>
      <div class="block-token">{token}</div>
      <div class="block-description">{description}</div>
      {items_html}
    </section>"""
        )
    return "".join(blocks)


def format_station_lookup_html(station_info: dict | None) -> str:
    if not station_info:
        return ""

    name = escape(str(station_info.get("name") or "Unknown"))
    icao = escape(str(station_info.get("icao") or station_info.get("station") or "Unknown"))
    iata = escape(str(station_info.get("iata") or "N/A"))
    city = escape(str(station_info.get("city") or "Unknown"))
    country = escape(str(station_info.get("country") or "Unknown"))
    elevation = station_info.get("elevation_ft")
    lat = station_info.get("latitude")
    lon = station_info.get("longitude")

    items = [
        ("ICAO", icao),
        ("IATA", iata),
        ("City", city),
        ("Country", country),
    ]
    if elevation is not None:
        items.append(("Elevation", escape(f"{elevation} ft")))
    if lat is not None and lon is not None:
        items.append(("Coordinates", escape(f"{lat}, {lon}")))

    rendered = "".join(
        f"""
        <div class="lookup-item">
          <div class="lookup-label">{label}</div>
          <div class="lookup-value">{value}</div>
        </div>"""
        for label, value in items
    )

    return f"""
        <section class="lookup-card">
          <div class="block-title">ICAO Lookup</div>
          <div class="block-token">{name}</div>
          <div class="block-description">Station information returned from the ICAO lookup feature.</div>
          <div class="lookup-grid">
            {rendered}
          </div>
        </section>"""


def render_page(
    metar: str = "",
    sections: list[dict[str, str]] | None = None,
    icao: str = "",
    station_search: str = "",
    search_selected_icao: str = "",
    search_selected_label: str = "",
    fetch_meta: str = "",
    error_message: str = "",
    station_info: dict | None = None,
    active_tab: str = "manual",
) -> str:
    metar_value = escape(metar)
    raw_metar_bar = (
        f"""
        <section class="raw-bar">
          <div class="raw-bar-label">Raw METAR</div>
          <div class="raw-bar-value">{metar_value}</div>
        </section>"""
        if metar.strip()
        else ""
    )
    icao_value = escape(icao)
    station_search_value = escape(station_search)
    search_selected_icao_value = escape(search_selected_icao)
    search_selected_label_value = escape(search_selected_label)
    block_html = render_blocks_html(sections or [])
    lookup_html = format_station_lookup_html(station_info)
    error_html = f'<div class="error-banner">{escape(error_message)}</div>' if error_message else ""
    fetch_html = f'<div class="fetch-meta">{escape(fetch_meta)}</div>' if fetch_meta else ""
    active_tab_value = escape(active_tab)
    refresh_icao = icao if active_tab == "live" else search_selected_icao if active_tab == "search" else ""
    refresh_icao_value = escape(refresh_icao)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>METAR</title>
  <style>
    :root {{
      color-scheme: light;
      --page: #4f8ae8;
      --page-2: #78aff7;
      --shell: rgba(255, 255, 255, 0.18);
      --surface: #fbfdff;
      --surface-2: #eef4fb;
      --surface-3: #f7faff;
      --text: #1a2232;
      --muted: #697386;
      --line: #dfe8f5;
      --accent: #2a66c8;
      --shadow: rgba(18, 37, 70, 0.18);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100svh;
      font-family: "Avenir Next", "Helvetica Neue", sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(255, 255, 255, 0.18), transparent 24%),
        linear-gradient(180deg, var(--page) 0%, var(--page-2) 100%);
      padding: 18px;
    }}
    main {{
      width: min(1280px, 100%);
      margin: 0 auto;
    }}
    .shell {{
      background: var(--shell);
      border-radius: 28px;
      padding: 14px;
      box-shadow: 0 26px 50px var(--shadow);
    }}
    .topbar {{
      display: grid;
      grid-template-columns: minmax(0, 1.1fr) minmax(0, 1.45fr);
      gap: 14px;
      align-items: stretch;
      margin-bottom: 14px;
    }}
    .hero,
    .composer {{
      background: var(--surface);
      border-radius: 22px;
      padding: 22px 24px;
      min-height: 100%;
      border: 1px solid rgba(255, 255, 255, 0.45);
    }}
    .eyebrow {{
      margin: 0 0 8px;
      font-size: 0.82rem;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: var(--accent);
      font-weight: 700;
    }}
    h1 {{
      margin: 0 0 10px;
      font-size: clamp(1.9rem, 3.6vw, 3rem);
      line-height: 0.95;
      letter-spacing: -0.04em;
      font-weight: 600;
    }}
    .intro {{
      margin: 0;
      color: var(--muted);
      line-height: 1.45;
      font-size: 1rem;
    }}
    form {{
      display: grid;
      gap: 12px;
      height: 100%;
    }}
    .composer-grid {{
      display: grid;
      gap: 14px;
    }}
    .tab-list {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
    }}
    .tab-button {{
      appearance: none;
      border: 1px solid var(--line);
      background: var(--surface-3);
      color: var(--muted);
      border-radius: 16px;
      padding: 14px 16px;
      text-align: left;
      cursor: pointer;
      font: inherit;
    }}
    .tab-button strong {{
      display: block;
      font-size: 0.98rem;
      color: var(--text);
      margin-bottom: 4px;
    }}
    .tab-button span {{
      display: block;
      font-size: 0.9rem;
      line-height: 1.35;
    }}
    .tab-button.is-active {{
      background: linear-gradient(135deg, rgba(36, 95, 191, 0.12), rgba(93, 151, 236, 0.16));
      border-color: rgba(42, 102, 200, 0.28);
      box-shadow: inset 0 0 0 1px rgba(42, 102, 200, 0.12);
    }}
    .field {{
      display: grid;
      gap: 8px;
    }}
    .field-card {{
      background: var(--surface-3);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 16px;
    }}
    .field-label {{
      font-size: 0.83rem;
      font-weight: 700;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}
    .field-note {{
      margin: 0;
      color: var(--muted);
      line-height: 1.4;
      font-size: 0.94rem;
    }}
    textarea {{
      width: 100%;
      min-height: 156px;
      resize: vertical;
      border-radius: 16px;
      border: 1px solid var(--line);
      padding: 16px 18px;
      font: inherit;
      font-size: 1.02rem;
      color: var(--text);
      background: var(--surface-3);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.7);
    }}
    input {{
      width: 100%;
      height: 52px;
      border-radius: 16px;
      border: 1px solid var(--line);
      padding: 0 16px;
      font: inherit;
      font-size: 1rem;
      color: var(--text);
      background: var(--surface-3);
    }}
    .fetch-card {{
      display: grid;
      gap: 12px;
      align-content: start;
    }}
    .panel {{
      display: none;
    }}
    .panel.is-active {{
      display: grid;
      gap: 12px;
    }}
    .search-dropdown {{
      display: none;
      margin-top: 2px;
      border: 1px solid var(--line);
      border-radius: 16px;
      background: white;
      overflow: hidden;
      max-height: 240px;
      overflow-y: auto;
    }}
    .search-dropdown.is-open {{
      display: block;
    }}
    .search-option {{
      width: 100%;
      border: 0;
      border-bottom: 1px solid var(--line);
      background: white;
      color: var(--text);
      text-align: left;
      padding: 14px 16px;
      cursor: pointer;
      font: inherit;
    }}
    .search-option:last-child {{
      border-bottom: 0;
    }}
    .search-option:hover {{
      background: var(--surface-3);
    }}
    .search-option-name {{
      font-weight: 700;
      margin-bottom: 4px;
    }}
    .search-option-meta {{
      font-size: 0.92rem;
      color: var(--muted);
      line-height: 1.35;
    }}
    .selected-station {{
      border-radius: 16px;
      background: #eff6ff;
      color: #1d4ed8;
      border: 1px solid #bfdbfe;
      padding: 14px 16px;
      font-size: 0.95rem;
      line-height: 1.4;
    }}
    button {{
      width: 100%;
      border: 0;
      border-radius: 18px;
      padding: 15px 18px;
      font: inherit;
      font-weight: 700;
      color: white;
      background: linear-gradient(135deg, #245fbf, #5d97ec);
      cursor: pointer;
      min-width: 170px;
    }}
    .error-banner,
    .fetch-meta {{
      border-radius: 16px;
      padding: 14px 16px;
      font-size: 0.95rem;
      line-height: 1.4;
    }}
    .error-banner {{
      background: #fff1f2;
      color: #9f1239;
      border: 1px solid #fecdd3;
    }}
    .fetch-meta {{
      background: #eff6ff;
      color: #1d4ed8;
      border: 1px solid #bfdbfe;
    }}
    .report {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 14px;
    }}
    .raw-bar {{
      grid-column: 1 / -1;
      display: grid;
      gap: 10px;
      background: linear-gradient(135deg, rgba(21, 37, 64, 0.94), rgba(32, 57, 94, 0.92));
      color: white;
      border-radius: 22px;
      padding: 18px 20px;
      box-shadow: 0 14px 28px rgba(21, 37, 64, 0.18);
    }}
    .raw-bar-label {{
      font-size: 0.82rem;
      text-transform: uppercase;
      letter-spacing: 0.12em;
      color: rgba(255, 255, 255, 0.72);
      font-weight: 700;
    }}
    .raw-bar-value {{
      font-size: 1rem;
      line-height: 1.5;
      letter-spacing: 0.02em;
      word-break: break-word;
      font-family: "SFMono-Regular", "Menlo", "Monaco", monospace;
    }}
    .lookup-card {{
      grid-column: 1 / -1;
      background: var(--surface);
      min-height: 0;
      padding: 22px;
      border-radius: 22px;
      border: 1px solid rgba(255, 255, 255, 0.52);
      box-shadow: 0 10px 24px rgba(35, 60, 105, 0.06);
    }}
    .lookup-grid {{
      margin-top: 18px;
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }}
    .lookup-item {{
      background: var(--surface-3);
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 14px;
    }}
    .lookup-label {{
      font-size: 0.82rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
      font-weight: 700;
      margin-bottom: 6px;
    }}
    .lookup-value {{
      font-size: 1rem;
      color: var(--text);
      line-height: 1.35;
      overflow-wrap: anywhere;
    }}
    .data-block {{
      background: var(--surface);
      min-height: 168px;
      padding: 22px 22px 20px;
      border-radius: 22px;
      border: 1px solid rgba(255, 255, 255, 0.52);
      display: flex;
      flex-direction: column;
      justify-content: flex-start;
      box-shadow: 0 10px 24px rgba(35, 60, 105, 0.06);
    }}
    .data-block-full {{
      grid-column: 1 / -1;
      min-height: 0;
    }}
    .block-title {{
      margin-bottom: 18px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      font-size: 0.88rem;
      font-weight: 700;
      color: #7e8796;
    }}
    .block-token {{
      font-size: clamp(1.4rem, 2.4vw, 2.1rem);
      font-weight: 600;
      line-height: 1.05;
      letter-spacing: -0.03em;
      margin-bottom: 8px;
      overflow-wrap: anywhere;
    }}
    .block-description {{
      font-size: 1rem;
      color: var(--muted);
      line-height: 1.38;
      max-width: none;
    }}
    .remark-list {{
      list-style: none;
      margin: 18px 0 0;
      padding: 0;
      display: grid;
      gap: 10px;
    }}
    .remark-item {{
      display: grid;
      grid-template-columns: minmax(140px, 220px) minmax(0, 1fr);
      gap: 14px;
      align-items: start;
      padding-top: 10px;
      border-top: 1px solid var(--line);
    }}
    .remark-token {{
      font-size: 0.95rem;
      font-weight: 700;
      color: var(--text);
      overflow-wrap: anywhere;
    }}
    .remark-text {{
      font-size: 0.97rem;
      color: var(--muted);
      line-height: 1.45;
    }}
    .empty-state {{
      grid-column: 1 / -1;
      min-height: 240px;
      display: grid;
      place-items: center;
      text-align: center;
      padding: 40px 28px;
      color: var(--muted);
      font-size: 1.15rem;
      background: var(--surface);
      border-radius: 22px;
    }}
    @media (max-width: 1100px) {{
      .report {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }}
    }}
    @media (max-width: 780px) {{
      body {{
        padding: 10px;
      }}
      .topbar {{
        grid-template-columns: 1fr;
      }}
      .hero,
      .composer {{
        padding: 18px;
      }}
      .tab-list {{
        grid-template-columns: 1fr;
      }}
      textarea {{
        min-height: 136px;
      }}
      .report {{
        grid-template-columns: 1fr;
      }}
      .lookup-grid {{
        grid-template-columns: 1fr;
      }}
      .remark-item {{
        grid-template-columns: 1fr;
        gap: 6px;
      }}
    }}
  </style>
</head>
<body>
  <main>
    <div class="shell">
      <section class="topbar">
        <div class="hero">
          <p class="eyebrow">Aviation Weather</p>
          <h1>METAR</h1>
          <p class="intro">A compact one-page layout with each decoded part shown as its own card.</p>
        </div>
        <section class="composer">
          <form method="post" id="metar-form">
            <input type="hidden" name="active_tab" id="active-tab" value="{active_tab_value}">
            <div class="composer-grid">
              <div class="tab-list">
                <button type="button" class="tab-button" data-tab="manual">
                  <strong>Manual METAR</strong>
                  <span>Paste a raw METAR string and decode it directly.</span>
                </button>
                <button type="button" class="tab-button" data-tab="live">
                  <strong>Live by ICAO</strong>
                  <span>Fetch current METAR and station data from a known ICAO code.</span>
                </button>
                <button type="button" class="tab-button" data-tab="search">
                  <strong>Search Airport</strong>
                  <span>Type an airport or city name, choose from the dropdown, then fetch live data.</span>
                </button>
              </div>
              <div class="field-card">
                <section class="panel" data-panel="manual">
                  <div class="field">
                    <div class="field-label">METAR Input</div>
                    <textarea name="metar" placeholder="Here to input METAR...">{metar_value}</textarea>
                  </div>
                  <button type="submit">Decode Typed METAR</button>
                </section>
                <section class="panel" data-panel="live">
                  <div class="fetch-card">
                    <div class="field">
                      <div class="field-label">ICAO Code</div>
                      <input name="icao" placeholder="KMCO" value="{icao_value}">
                    </div>
                    <p class="field-note">Uses the token from your local <code>.env</code> or environment variables automatically. Refreshes every {AUTO_REFRESH_SECONDS} seconds while active.</p>
                    <button type="submit">Fetch Live METAR</button>
                  </div>
                </section>
                <section class="panel" data-panel="search">
                  <div class="fetch-card">
                    <div class="field">
                      <div class="field-label">Airport or City</div>
                      <input id="station-search-input" name="station_search" placeholder="Orlando" value="{station_search_value}" autocomplete="off">
                      <input type="hidden" id="selected-icao" name="search_selected_icao" value="{search_selected_icao_value}">
                      <input type="hidden" id="selected-label" name="search_selected_label" value="{search_selected_label_value}">
                      <div id="search-dropdown" class="search-dropdown"></div>
                    </div>
                    <div id="selected-station" class="selected-station"{" style='display:none;'" if not search_selected_label else ""}>Selected: {search_selected_label_value}</div>
                    <p class="field-note">Search updates live as you type. After you pick a station, live data refreshes every {AUTO_REFRESH_SECONDS} seconds.</p>
                    <button type="submit">Fetch Selected Airport</button>
                  </div>
                </section>
                {error_html}
                {fetch_html}
              </div>
            </div>
          </form>
        </section>
      </section>
      <section class="report">
        {raw_metar_bar}
        {lookup_html}
        {block_html if sections else '<div class="empty-state">Your decoded METAR blocks will appear here.</div>'}
      </section>
    </div>
  </main>
  <form method="post" id="auto-refresh-form" style="display:none">
    <input type="hidden" name="active_tab" value="{active_tab_value}">
    <input type="hidden" name="icao" value="{refresh_icao_value}">
    <input type="hidden" name="search_selected_icao" value="{refresh_icao_value}">
  </form>
  <script>
    const activeTabInput = document.getElementById('active-tab');
    const tabButtons = Array.from(document.querySelectorAll('.tab-button'));
    const panels = Array.from(document.querySelectorAll('.panel'));
    const currentTab = activeTabInput.value || 'manual';

    function activateTab(tab) {{
      activeTabInput.value = tab;
      tabButtons.forEach((button) => {{
        button.classList.toggle('is-active', button.dataset.tab === tab);
      }});
      panels.forEach((panel) => {{
        panel.classList.toggle('is-active', panel.dataset.panel === tab);
      }});
    }}

    tabButtons.forEach((button) => {{
      button.addEventListener('click', () => activateTab(button.dataset.tab));
    }});
    activateTab(currentTab);

    const searchInput = document.getElementById('station-search-input');
    const selectedIcao = document.getElementById('selected-icao');
    const selectedLabel = document.getElementById('selected-label');
    const selectedStation = document.getElementById('selected-station');
    const searchDropdown = document.getElementById('search-dropdown');
    let searchTimer = null;

    function setSelectedStation(label, icao) {{
      selectedLabel.value = label || '';
      selectedIcao.value = icao || '';
      if (label) {{
        selectedStation.textContent = 'Selected: ' + label;
        selectedStation.style.display = 'block';
      }} else {{
        selectedStation.textContent = '';
        selectedStation.style.display = 'none';
      }}
    }}

    function renderSearchResults(results) {{
      if (!results.length) {{
        searchDropdown.innerHTML = '';
        searchDropdown.classList.remove('is-open');
        return;
      }}
      searchDropdown.innerHTML = results.map((item) => {{
        const label = `${{item.name}} (${{item.icao}}${{item.iata ? ' / ' + item.iata : ''}})`;
        const meta = `${{item.city || 'Unknown'}}, ${{item.country || 'Unknown'}}`;
        return `
          <button type="button" class="search-option" data-icao="${{item.icao || item.station || ''}}" data-label="${{label}}">
            <div class="search-option-name">${{item.name || 'Unknown'}}</div>
            <div class="search-option-meta">ICAO ${{item.icao || item.station || 'Unknown'}} | IATA ${{item.iata || 'N/A'}}</div>
            <div class="search-option-meta">${{meta}}</div>
          </button>
        `;
      }}).join('');
      searchDropdown.classList.add('is-open');
      searchDropdown.querySelectorAll('.search-option').forEach((button) => {{
        button.addEventListener('click', () => {{
          searchInput.value = button.dataset.label;
          setSelectedStation(button.dataset.label, button.dataset.icao);
          searchDropdown.classList.remove('is-open');
        }});
      }});
    }}

    if (searchInput) {{
      searchInput.addEventListener('input', () => {{
        setSelectedStation('', '');
        const value = searchInput.value.trim();
        window.clearTimeout(searchTimer);
        if (value.length < 2) {{
          searchDropdown.innerHTML = '';
          searchDropdown.classList.remove('is-open');
          return;
        }}
        searchTimer = window.setTimeout(async () => {{
          try {{
            const response = await fetch(`/api/search-station?text=${{encodeURIComponent(value)}}`);
            const data = await response.json();
            renderSearchResults(Array.isArray(data.results) ? data.results : []);
          }} catch (_error) {{
            searchDropdown.innerHTML = '';
            searchDropdown.classList.remove('is-open');
          }}
        }}, 350);
      }});
    }}

    if (selectedLabel.value && selectedIcao.value) {{
      setSelectedStation(selectedLabel.value, selectedIcao.value);
    }}

    const refreshForm = document.getElementById('auto-refresh-form');
    const refreshTab = activeTabInput.value;
    const refreshIcao = refreshTab === 'live'
      ? refreshForm.querySelector('input[name="icao"]').value
      : refreshForm.querySelector('input[name="search_selected_icao"]').value;
    if (refreshForm && refreshIcao && (refreshTab === 'live' || refreshTab === 'search')) {{
      window.setTimeout(() => refreshForm.submit(), {AUTO_REFRESH_SECONDS * 1000});
    }}
  </script>
</body>
</html>"""


class MetarHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/search-station":
            self._handle_station_search_api(parsed)
            return
        self._send_html(render_page())

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length).decode("utf-8")
        form = parse_qs(body)
        metar = form.get("metar", [""])[0].strip()
        icao = form.get("icao", [""])[0].strip().upper()
        active_tab = form.get("active_tab", ["manual"])[0].strip() or "manual"
        station_search = form.get("station_search", [""])[0].strip()
        search_selected_icao = form.get("search_selected_icao", [""])[0].strip().upper()
        search_selected_label = form.get("search_selected_label", [""])[0].strip()
        error_message = ""
        fetch_meta = ""
        station_info = None

        action = "manual"
        current_icao = ""
        if active_tab == "live":
            action = "live"
            current_icao = icao
        elif active_tab == "search":
            action = "live"
            current_icao = search_selected_icao

        allowed, retry_after = check_rate_limit(self, action)
        if not allowed:
            error_message = f"Too many requests. Please wait about {retry_after} seconds and try again."
            sections = parse_metar_sections(metar) if metar else []
            self._send_html(
                render_page(
                    metar,
                    sections,
                    icao,
                    station_search,
                    search_selected_icao,
                    search_selected_label,
                    fetch_meta,
                    error_message,
                    station_info,
                    active_tab,
                ),
                status=429,
                extra_headers={"Retry-After": str(retry_after)},
            )
            return

        if current_icao:
            try:
                station_info = cached_station_data(current_icao)
                data = cached_metar_data(current_icao)
                metar = str(data.get("raw", "")).strip()
                observed = str(data.get("time", {}).get("repr", "")).strip() if isinstance(data.get("time"), dict) else ""
                flight_rules = str(data.get("flight_rules", "")).strip()
                fetch_meta = f"Fetched live METAR for {current_icao}" + (
                    f" | Time {observed}" if observed else ""
                ) + (
                    f" | Flight rules {flight_rules}" if flight_rules else ""
                )
            except AvwxError as exc:
                error_message = public_error_message(exc)

        sections = parse_metar_sections(metar) if metar else []
        self._send_html(
            render_page(
                metar,
                sections,
                icao,
                station_search,
                search_selected_icao,
                search_selected_label,
                fetch_meta,
                error_message,
                station_info,
                active_tab,
            )
        )

    def _handle_station_search_api(self, parsed) -> None:
        params = parse_qs(parsed.query)
        text = params.get("text", [""])[0].strip()
        results = []
        error = ""
        allowed, retry_after = check_rate_limit(self, "search")
        if not allowed:
            payload = json.dumps(
                {
                    "results": [],
                    "error": f"Too many requests. Please wait about {retry_after} seconds and try again.",
                }
            ).encode("utf-8")
            self.send_response(429)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Retry-After", str(retry_after))
            self.end_headers()
            self.wfile.write(payload)
            return
        if text:
            try:
                results = cached_search_station_data(text)[:8]
            except AvwxError as exc:
                error = public_error_message(exc)
        payload = json.dumps({"results": results, "error": error}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:
        return

    def _send_html(
        self,
        html: str,
        status: int = 200,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        payload = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), MetarHandler)
    print(f"METAR web app running at http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
