import argparse
import json
from pathlib import Path

from .avwx_client import AvwxError, fetch_metar_data, fetch_raw_metar, fetch_station_data, search_station_data
from .metar_decoder import decode_metar


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Decode METAR strings into plain English.")
    parser.add_argument("metar", nargs="?", help="A single METAR string to decode.")
    parser.add_argument(
        "-f",
        "--file",
        type=Path,
        help="Read one METAR per line from a text file.",
    )
    parser.add_argument("--icao", help="Fetch the current METAR for an ICAO station via AVWX.")
    parser.add_argument("--lookup-icao", help="Look up station information for an ICAO code via AVWX.")
    parser.add_argument("--search-station", help="Search stations by airport name or city via AVWX.")
    parser.add_argument("--token", help="AVWX API token. Falls back to .env or AVWX_TOKEN.")
    parser.add_argument(
        "--json",
        action="store_true",
        help="When used with --icao, print the full AVWX JSON response.",
    )
    return parser


def decode_file(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        metar = line.strip()
        if not metar:
            continue
        print(f"METAR: {metar}")
        print(f"Decoded: {decode_metar(metar)}")
        print("-" * 80)


def print_station_lookup(data: dict) -> None:
    name = data.get("name") or "Unknown"
    icao = data.get("icao") or data.get("station") or "Unknown"
    iata = data.get("iata") or "N/A"
    city = data.get("city") or "Unknown"
    country = data.get("country") or "Unknown"
    elevation = data.get("elevation_ft")
    lat = data.get("latitude")
    lon = data.get("longitude")

    print(f"Station: {name}")
    print(f"ICAO: {icao}")
    print(f"IATA: {iata}")
    print(f"Location: {city}, {country}")
    if elevation is not None:
        print(f"Elevation: {elevation} ft")
    if lat is not None and lon is not None:
        print(f"Coordinates: {lat}, {lon}")


def print_station_search(results: list[dict]) -> None:
    if not results:
        print("No station matches found.")
        return

    for index, item in enumerate(results, start=1):
        name = item.get("name") or "Unknown"
        icao = item.get("icao") or item.get("station") or "Unknown"
        iata = item.get("iata") or "N/A"
        city = item.get("city") or "Unknown"
        country = item.get("country") or "Unknown"
        print(f"{index}. {name}")
        print(f"   ICAO: {icao} | IATA: {iata}")
        print(f"   Location: {city}, {country}")


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.search_station:
        try:
            results = search_station_data(args.search_station, args.token)
            if args.json:
                print(json.dumps(results, indent=2, ensure_ascii=False))
            else:
                print_station_search(results)
        except AvwxError as exc:
            parser.error(str(exc))
        return

    if args.lookup_icao:
        try:
            if args.json:
                print(json.dumps(fetch_station_data(args.lookup_icao, args.token), indent=2, ensure_ascii=False))
            else:
                print_station_lookup(fetch_station_data(args.lookup_icao, args.token))
        except AvwxError as exc:
            parser.error(str(exc))
        return

    if args.icao:
        try:
            if args.json:
                print(json.dumps(fetch_metar_data(args.icao, args.token), indent=2, ensure_ascii=False))
            else:
                raw_metar = fetch_raw_metar(args.icao, args.token)
                print(f"Fetched METAR: {raw_metar}")
                print(decode_metar(raw_metar))
        except AvwxError as exc:
            parser.error(str(exc))
        return

    if args.file:
        decode_file(args.file)
        return

    if args.metar:
        print(decode_metar(args.metar))
        return

    metar_input = input("Enter METAR: ").strip()
    print(decode_metar(metar_input))


if __name__ == "__main__":
    main()
