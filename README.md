# METAR

METAR is a small METAR decoder with:

- a local web UI
- a CLI
- live AVWX lookup by ICAO
- airport / city search

## Setup

Create a `.env` file in the project root:

```env
AVWX_TOKEN=your_avwx_token_here
```

## Run

Web:

```bash
python3 -m metar.web
```

Open:

```text
http://127.0.0.1:8000
```

CLI:

```bash
python3 -m metar.cli "METAR KMCO 252253Z 08010KT 10SM SCT032 SCT070 24/18 A3011 RMK AO2 SLP193 T02440178"
```

Live METAR by ICAO:

```bash
python3 -m metar.cli --icao KMCO
```

Station search:

```bash
python3 -m metar.cli --search-station Orlando
```

## Docker

```bash
docker compose up --build
```
