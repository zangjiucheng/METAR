# METAR

METAR is a lightweight weather decoder with:

- a local web UI
- a CLI
- live AVWX lookup by ICAO
- airport and city search

## Live Demo

https://metar.jiucheng-zang.ca

## Quick Start

Run the web app locally:

```bash
python3 -m metar.web
```

Then open:

```text
http://127.0.0.1:8000
```

Basic CLI usage:

```bash
python3 -m metar.cli "METAR KMCO 252253Z 08010KT 10SM SCT032 SCT070 24/18 A3011 RMK AO2 SLP193 T02440178"
```

Fetch live METAR by ICAO:

```bash
python3 -m metar.cli --icao KMCO
```

Search stations by airport or city:

```bash
python3 -m metar.cli --search-station Orlando
```

## Docker

Create a `.env` file in the project root:

```env
AVWX_TOKEN=your_avwx_token_here
```

Start with Docker Compose:

```bash
docker compose up -d
```

The default image is:

```text
ghcr.io/zangjiucheng/metar:latest
```

You can also override it:

```bash
METAR_IMAGE=ghcr.io/zangjiucheng/metar:latest docker compose up -d
```
