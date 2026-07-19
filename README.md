# EUDR Platform

Compliance automation for the EU Deforestation Regulation (Regulation (EU)
2023/1115). The platform helps operators and traders that place regulated
commodities on the EU market — and the upstream exporters that supply them —
produce and file a Due Diligence Statement (DDS) in the EU TRACES system.

The regulation requires, per shipment or per annual declaration, plot-level
geolocation of where each commodity was produced, evidence that the land was
not deforested after 31 December 2020, evidence of legal production, and a
documented risk assessment. The heavy lift is multi-party data collection and
verification, not the filing click. This platform automates that pipeline.

## What it does

The build is organised in four phases:

1. **Foundation** — FastAPI backend, SQLAlchemy data model for the full
   pipeline (`client`, `supplier`, `plot`, `document`, `shipment`, `dds`,
   `evidence`, `outreach_message`, `alert`), configuration and database layer.
2. **Plot checker** (lead magnet) — upload coordinates or GeoJSON, get a
   deforestation risk report (green / amber / red) built from a
   convergence-of-evidence overlay against public forest datasets. PDF export
   is gated behind email capture.
3. **Scope checker** — determine whether a product falls under EUDR by CN /
   HS code and free-text description, returning scope, country risk tier and
   the documentation required.
4. **DDS assembly pipeline** — onboard clients, suppliers, plots (GeoJSON /
   CSV / WKT / KML / shapefile) and shipments through the web UI, assess each
   plot's deforestation risk, link plots to shipments, assemble a DDS payload
   with an internal reference, and export it as PDF. Submission to TRACES is
   not integrated yet (the official DDS schema is unverified), so the DDS is
   an internal, TRACES-mappable record.

## Design principles

- **Deterministic core, LLM at the edges.** Geometry validation, risk scoring
  thresholds and scope determination are deterministic code with versioned
  rule tables. Language models are only ever used for extraction and drafting,
  never to decide a green/amber/red call or a scope flag.
- **Fail loud.** Invalid geometry, unreachable data providers or missing
  evidence raise explicit errors. There is no silent fallback and no
  placeholder data.
- **Evidence first.** Every check writes an auditable artifact; a red plot
  blocks DDS assembly for any batch that contains it.

## Stack

- Python 3.11+, FastAPI, Uvicorn
- SQLAlchemy 2.x (SQLite in development, PostgreSQL in production), Alembic
- Shapely + pyproj for geometry (WGS84 normalisation, validation, overlap)
- httpx clients for the Open Foris Whisp API and the GFW Data API
- Jinja + HTMX + Tailwind for a server-rendered frontend (no SPA)
- pytest for tests

## Getting started

```bash
python3 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # adjust as needed; SQLite is the dev default
uvicorn app.main:app --reload
```

Then open <http://localhost:8000>.

In development the schema is created automatically on startup. Production
applies Alembic migrations instead (see `migrations/README`):

```bash
alembic upgrade head
```

### Checks

```bash
ruff check .          # lint
mypy app              # type check
pytest                # tests
```

## Data providers

Deforestation analysis calls the Open Foris **Whisp API** (FAO) and, as a
secondary source, the **GFW Data API**. No raster tiles are stored locally;
zonal statistics are fetched per plot. Provider URLs and keys are configured
in `.env` (see `.env.example`).

## Licence

Proprietary. See [LICENSE](LICENSE).
