# Whisp integration

Stage 4 of the plot-checker pipeline (`parse -> validate -> analyze -> assess`)
gets its deforestation evidence from the Open Foris **Whisp API** (FAO),
`https://whisp.openforis.org/api`, API **v2.1.0**, backed by the
[`openforis-whisp`](https://pypi.org/project/openforis-whisp/) library over
Google Earth Engine. The client is `app/services/whisp.py`; the column-to-signal
mapping it feeds is `WHISP_COLUMN_FAMILIES` in `app/services/risk.py`.

## Getting an API key (the one manual step)

Every `/submit/*` and `/status/*` call needs an `x-api-key` header. Keys are
free but issued through a web form, not an API, so a human must do this once:

1. Open <https://whisp.openforis.org/register> and register for a key.
2. Put it in `.env`: `WHISP_API_KEY=<the key>`.
3. Restart the app. Until then, both the public plot-checker and the per-plot
   assess button fail loud: `RiskProviderNotConfigured` -> HTTP 503. That is the
   intended state without a key, not a bug — no fabricated verdict is ever
   returned.

An absent **or** invalid/expired key both surface as `RiskProviderNotConfigured`
(HTTP 503); the message quotes the server's `auth_missing_api_key` /
`auth_invalid_api_key` code.

## Verified API contract (live probe, 2026-07)

The response shapes below were confirmed against the live API. The submit
happy-path body is the API's own OpenAPI example (authoritative); the error
envelopes and status codes were observed directly.

### Sync submit — what the client uses

The client takes the **synchronous** path: one call returns the result, no job
token, no polling.

```
POST /api/submit/geojson
Header: x-api-key: <key>
Body:   { "type": "FeatureCollection",
          "features": [ { "type": "Feature", "geometry": <geom>, "properties": {...} } ],
          "analysisOptions": { "async": false, "unitType": "ha", "externalIdColumn": "external_ref" } }
```

`GET /api/config` bounds the sync path at **250 geometries / 60s**
(`geometryLimitSync`, `analysisTimeoutSyncSeconds`) — ample for the
one-plot-at-a-time use here. The async path (token + `GET /status/{token}` +
`GET /generate-geojson/{token}`) exists for large batches and is deliberately
**not** implemented; add it as a separate feature if batch analysis is ever
needed, don't resurrect polling for single plots.

**`unitType: "ha"` is mandatory.** The risk engine's thresholds
(`MIN_SIGNAL_HA`, `MIN_FOREST_HA`) and every `DatasetSignal.value` are in
hectares; any other unit would silently corrupt the verdict.

### Response envelope

Every response — success or error — is the same shape:

```json
{ "code": "<SystemCode>", "message": "...", "cause": "... | null", "data": <any> }
```

* **Success**: HTTP 200, `code == "analysis_completed"`, `data` a GeoJSON
  `FeatureCollection`. The first feature's `properties` holds the result columns
  (`EUFO_2020`, `GFC_loss_after_2020`, ...) consumed by
  `evidence_from_whisp_properties`.
* **Errors**: non-2xx with the same envelope. Observed:
  `401 auth_missing_api_key`, `401 auth_invalid_api_key`,
  `404 analysis_job_not_found`. Other `SystemCode`s from the spec include
  `validation_*` (4xx), `analysis_error` / `analysis_timeout` / `analysis_cancelled`,
  and `system_internal_server_error` (5xx).

### Result columns

The full result-field catalogue is public (no key needed):
`GET /api/result-fields/lookup-datasets` (CSV). The columns are grouped by
`theme`: `treecover` (2020 baseline forest), `commodities`, `disturbance_before`
(per-year + `*_before_2020`), and `disturbance_after` (per-year + the aggregate
`*_after_2020` columns the risk engine keys on: `GFC_loss_after_2020`,
`TMF_def_after_2020`, `RADD_after_2020`, `GLAD-L_after_2020`, ...). The
`WHISP_COLUMN_FAMILIES` mapping was reconciled against this catalogue and its
column names are correct.

## Still needs a live key to confirm

The happy-path shape is trusted from the OpenAPI example, but two things can
only be pinned down with a real key against a real plot:

* the **exact numeric magnitudes** per column under `unitType: "ha"` (used to
  sanity-check `MIN_SIGNAL_HA` / `MIN_FOREST_HA` against real sub-pixel noise);
* whether any mapped column is ever returned as a **string** rather than a
  number (the client fails loud on non-numeric mapped columns, by design).

Once a key is set, run one known plot end to end and eyeball the stored
`Evidence.data_json` before trusting production verdicts.
