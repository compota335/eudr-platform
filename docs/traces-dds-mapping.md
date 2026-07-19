# DDS payload → TRACES mapping

This document maps every field of the internal DDS payload produced by
`app/services/dds_assembly.py` (`build_dds_payload` / `assemble_dds`) to the
DDS field it is intended to feed in the EU TRACES system.

> **The exact TRACES EUDR API JSON schema is UNVERIFIED** (flagged in the domain
> deep dive, Section 9). This internal record is *designed* to be mappable to a
> TRACES Due Diligence Statement, but **no submission to TRACES is performed**
> by this service. In particular, `reference_number` is an **INTERNAL** id we
> generate at assembly time — it is **not** a TRACES reference number. The
> TRACES-issued `verification_number` and `traces_reference` are populated
> **only** by a future, verified TRACES integration; until then they are `None`.

## Regulatory basis

Under Regulation (EU) 2023/1115 (the EUDR), the Due Diligence Statement carried
by an operator placing an in-scope commodity on the EU market declares:

- **Operator identity** — who is filing.
- **Commodity + CN code + quantity** — what is being placed on the market.
- **Country of production** — where the commodity was produced.
- **Geolocation of every plot of land** the commodity came from: a **polygon**
  for plots of **4 ha or more**, a single **point** allowed only **under 4 ha**,
  at **6-decimal** precision.
- A **deforestation-free** assertion against the **2020-12-31** cutoff.

On successful submission, **TRACES returns a reference number and a
verification number**. Those two values are the only fields that a TRACES round
trip adds; everything else in the table below is data we supply.

## Field mapping

| Payload field | Intended TRACES DDS field | Status |
| --- | --- | --- |
| `schema_version` | (none — internal payload version tag) | internal-only |
| `reference_number` | DDS reference number | **UNKNOWN** — internal id at assembly; a real TRACES reference is issued only on verified submission |
| `operator.name` | Operator / trader name | mapped |
| `operator.country` | Operator country | mapped |
| `operator.eori` | Operator EORI number | mapped |
| `operator.contact_email` | Operator contact e-mail | mapped |
| `commodity.commodity` | Relevant commodity (Annex I) | mapped |
| `commodity.cn_code` | Combined Nomenclature (CN/HS) code | mapped |
| `commodity.quantity_kg` | Net mass / quantity (kg) | mapped |
| `commodity.country_of_production` | Country of production | mapped |
| `scope.in_scope` | (none — internal Annex I scope gate) | internal-only |
| `scope.matched_cn` | (supports the CN code declaration) | internal-only |
| `scope.country_risk` | (Article 29 country benchmarking tier) | **UNKNOWN** — country benchmarking itself unverified (deep dive Section 9) |
| `scope.cn_table_version` | (none — reproducibility metadata) | internal-only |
| `scope.country_table_version` | (none — reproducibility metadata) | internal-only |
| `geolocation[].external_ref` | Plot / production place identifier | mapped |
| `geolocation[].supplier_name` | Production place / producer name | mapped |
| `geolocation[].country` | Plot country | mapped |
| `geolocation[].commodity` | Plot commodity | mapped |
| `geolocation[].area_ha` | Plot area (ha) — drives point-vs-polygon rule | mapped |
| `geolocation[].geometry_type` | Geometry type (Point / Polygon / MultiPolygon) | mapped |
| `geolocation[].geometry` | Geolocation coordinates (GeoJSON, WGS84, 6-decimal) | mapped |
| `geolocation[].centroid` | (derived — plot centroid for listing/maps) | internal-only |
| `geolocation[].risk_level` | (internal deforestation verdict; RED blocks filing) | internal-only |
| `deforestation_free.cutoff_date` | Deforestation-free cutoff (2020-12-31) | mapped |
| `deforestation_free.ruleset_version` | (none — reproducibility metadata) | internal-only |
| `deforestation_free.assertion` | Deforestation-free declaration | mapped |
| *(TRACES-issued)* `verification_number` | DDS verification number | **UNKNOWN** — populated only by a future verified TRACES integration |
| *(TRACES-issued)* `traces_reference` | TRACES system reference | **UNKNOWN** — populated only by a future verified TRACES integration |

## Notes

- **Precision.** Geometry is stored as WGS84 GeoJSON. The EUDR requires
  6-decimal precision; that is enforced upstream in the plot-checker pipeline
  (parse/validate), not re-imposed here.
- **Point-vs-polygon.** `assemble_dds` enforces the EUDR rule that a plot of
  `>= 4 ha` must be a `Polygon` or `MultiPolygon` and refuses to assemble
  otherwise (`DDSIncompleteError`).
- **Red plots.** A shipment containing any plot assessed `RED` is refused
  (`DDSBlockedError`); a DDS is never filed over a red plot.
- **No fabricated TRACES data.** Because the TRACES schema is unverified, this
  service stops at an internal, mappable record. The `verification_number` and
  `traces_reference` columns exist on the `DDS` row but remain `None` until a
  verified TRACES submission fills them.
