import json
import os
import threading
import time
from collections import deque
from functools import lru_cache
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .avwx_client import AvwxError, fetch_metar_data, fetch_station_data, search_station_data
from .metar_decoder import parse_metar_sections

HOST = os.getenv("METAR_HOST", "0.0.0.0")
PORT = int(os.getenv("METAR_PORT", "8000"))
AUTO_REFRESH_SECONDS = 60
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOGO_PATH = PROJECT_ROOT / "logo.png"
TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "index.html"
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


@lru_cache(maxsize=1)
def load_html_template() -> str:
    return TEMPLATE_PATH.read_text(encoding="utf-8")


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
    selected_station_style = "" if search_selected_label else " style='display:none;'"
    empty_or_blocks = block_html if sections else '<div class="empty-state">Your decoded METAR blocks will appear here.</div>'
    html = load_html_template()
    replacements = {
        "__ACTIVE_TAB_VALUE__": active_tab_value,
        "__AUTO_REFRESH_SECONDS__": str(AUTO_REFRESH_SECONDS),
        "__AUTO_REFRESH_MILLISECONDS__": str(AUTO_REFRESH_SECONDS * 1000),
        "__BLOCK_HTML__": empty_or_blocks,
        "__ERROR_HTML__": error_html,
        "__FETCH_HTML__": fetch_html,
        "__ICAO_VALUE__": icao_value,
        "__LOOKUP_HTML__": lookup_html,
        "__METAR_VALUE__": metar_value,
        "__RAW_METAR_BAR__": raw_metar_bar,
        "__REFRESH_ICAO_VALUE__": refresh_icao_value,
        "__SEARCH_SELECTED_ICAO_VALUE__": search_selected_icao_value,
        "__SEARCH_SELECTED_LABEL_VALUE__": search_selected_label_value,
        "__SELECTED_STATION_STYLE__": selected_station_style,
        "__STATION_SEARCH_VALUE__": station_search_value,
    }
    for key, value in replacements.items():
        html = html.replace(key, value)
    return html


class MetarHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/logo.png":
            self._send_file(LOGO_PATH, "image/png")
            return
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

    def _send_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self.send_error(404)
            return
        payload = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(payload)


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), MetarHandler)
    print(f"METAR web app running at http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
