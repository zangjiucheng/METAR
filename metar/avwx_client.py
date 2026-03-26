from pathlib import Path
import json
import os
from typing import Any, Dict, List
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

METAR_API_BASE = "https://avwx.rest/api/metar/"
STATION_API_BASE = "https://avwx.rest/api/station/"
STATION_SEARCH_API_BASE = "https://avwx.rest/api/search/station"
TOKEN_ENV_VARS = ("AVWX_TOKEN", "AVWX_API_TOKEN")
DOTENV_PATH = Path(__file__).resolve().parent.parent / ".env"


class AvwxError(Exception):
    pass


def load_dotenv(path: Path = DOTENV_PATH) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if not key or key in os.environ:
            continue

        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]

        os.environ[key] = value


def resolve_token(token: str | None = None) -> str:
    if token:
        return token.strip()

    load_dotenv()

    for name in TOKEN_ENV_VARS:
        value = os.getenv(name, "").strip()
        if value:
            return value
    raise AvwxError("Missing AVWX API token. Set it in .env, AVWX_TOKEN, or provide --token.")


def fetch_json(url: str, token: str | None = None) -> Any:
    auth_token = resolve_token(token)
    request = Request(
        url,
        headers={
            "Authorization": "BEARER " + auth_token,
            "Accept": "application/json",
        },
    )

    try:
        with urlopen(request, timeout=15) as response:
            payload = response.read().decode("utf-8")
    except HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace").strip()
        raise AvwxError(f"AVWX request failed with HTTP {exc.code}: {details}") from exc
    except URLError as exc:
        raise AvwxError(f"AVWX request failed: {exc.reason}") from exc

    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise AvwxError("AVWX returned invalid JSON.") from exc

    return data


def fetch_metar_data(icao: str, token: str | None = None) -> Dict[str, Any]:
    station = icao.strip().upper()
    if not station:
        raise AvwxError("ICAO code is required.")
    data = fetch_json(METAR_API_BASE + station, token)
    if not isinstance(data, dict):
        raise AvwxError("AVWX returned an unexpected METAR response shape.")
    return data


def fetch_station_data(icao: str, token: str | None = None) -> Dict[str, Any]:
    station = icao.strip().upper()
    if not station:
        raise AvwxError("ICAO code is required.")
    data = fetch_json(STATION_API_BASE + station, token)
    if not isinstance(data, dict):
        raise AvwxError("AVWX returned an unexpected station response shape.")
    return data


def search_station_data(query: str, token: str | None = None) -> List[Dict[str, Any]]:
    text = query.strip()
    if not text:
        raise AvwxError("Station search text is required.")
    data = fetch_json(STATION_SEARCH_API_BASE + "?" + urlencode({"text": text}), token)
    if not isinstance(data, list):
        raise AvwxError("AVWX returned an unexpected station search response shape.")
    return [item for item in data if isinstance(item, dict)]


def fetch_raw_metar(icao: str, token: str | None = None) -> str:
    data = fetch_metar_data(icao, token)
    raw = data.get("raw")
    if not isinstance(raw, str) or not raw.strip():
        raise AvwxError("AVWX response did not contain a raw METAR.")
    return raw.strip()
