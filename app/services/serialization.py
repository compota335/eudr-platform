"""Lossless JSON (de)serialization of a :class:`RiskResult`.

A ``PlotCheck`` row stores the full verdict as JSON in ``result_json`` so the
gated PDF can be rebuilt later without re-calling the deforestation provider.
The verdict is a nested tree of frozen dataclasses (``RiskResult`` -> nested
``PlotEvidence`` -> tuples of ``DatasetSignal``) plus a ``RiskLevel`` enum, so a
naive ``json.dumps`` would lose the enum type and the tuple/dataclass shapes.

These helpers round-trip that tree faithfully: ``deserialize_risk_result`` of
``serialize_risk_result`` reconstructs an equal object (tested). They fail loud:
a payload missing a required key or carrying an unknown ``RiskLevel`` raises
``ValueError`` rather than silently substituting a default.
"""

from __future__ import annotations

import json
from typing import Any

from app.geo.schemas import DatasetSignal, PlotEvidence, RiskResult
from app.models.enums import RiskLevel

# The four ``DatasetSignal`` tuple fields on ``PlotEvidence``, serialized as
# arrays of signal objects and rebuilt in the same order.
_SIGNAL_FIELDS: tuple[str, ...] = (
    "forest_2020",
    "loss_after_2020",
    "loss_after_2020_buffer",
    "commodity_2020",
)


def serialize_risk_result(result: RiskResult) -> str:
    """Serialize a :class:`RiskResult` to a JSON string, losslessly."""
    return json.dumps(_risk_result_to_dict(result), separators=(",", ":"))


def deserialize_risk_result(json_str: str) -> RiskResult:
    """Rebuild a :class:`RiskResult` from a string produced by :func:`serialize_risk_result`.

    Raises:
        ValueError: the JSON is malformed, a required key is missing, or the
            recorded ``level`` is not a known :class:`RiskLevel`.
    """
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid RiskResult JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(
            f"RiskResult JSON must be an object, got {type(data).__name__}"
        )
    return _risk_result_from_dict(data)


# --------------------------------------------------------------------------- #
# Internal: dataclass <-> plain dict                                           #
# --------------------------------------------------------------------------- #
def _risk_result_to_dict(result: RiskResult) -> dict[str, Any]:
    return {
        "level": result.level.value,
        "ruleset_version": result.ruleset_version,
        "forest_2020_present": result.forest_2020_present,
        "signals_in_plot": result.signals_in_plot,
        "signals_in_buffer": result.signals_in_buffer,
        "converging_families": list(result.converging_families),
        "rationale": list(result.rationale),
        "evidence": _evidence_to_dict(result.evidence),
    }


def _risk_result_from_dict(data: dict[str, Any]) -> RiskResult:
    return RiskResult(
        level=_level_from_value(_require(data, "level")),
        ruleset_version=_require(data, "ruleset_version"),
        forest_2020_present=bool(_require(data, "forest_2020_present")),
        signals_in_plot=int(_require(data, "signals_in_plot")),
        signals_in_buffer=int(_require(data, "signals_in_buffer")),
        converging_families=tuple(_require(data, "converging_families")),
        rationale=tuple(_require(data, "rationale")),
        evidence=_evidence_from_dict(_require(data, "evidence")),
    )


def _evidence_to_dict(evidence: PlotEvidence) -> dict[str, Any]:
    payload: dict[str, Any] = {
        field: [_signal_to_dict(s) for s in getattr(evidence, field)]
        for field in _SIGNAL_FIELDS
    }
    payload["provider"] = evidence.provider
    payload["dataset_versions"] = evidence.dataset_versions
    payload["raw"] = evidence.raw
    return payload


def _evidence_from_dict(data: dict[str, Any]) -> PlotEvidence:
    if not isinstance(data, dict):
        raise ValueError(
            f"evidence must be an object, got {type(data).__name__}"
        )
    signals = {
        field: tuple(_signal_from_dict(s) for s in data.get(field, ()))
        for field in _SIGNAL_FIELDS
    }
    return PlotEvidence(
        forest_2020=signals["forest_2020"],
        loss_after_2020=signals["loss_after_2020"],
        loss_after_2020_buffer=signals["loss_after_2020_buffer"],
        commodity_2020=signals["commodity_2020"],
        provider=data.get("provider", ""),
        dataset_versions=data.get("dataset_versions", {}),
        raw=data.get("raw", {}),
    )


def _signal_to_dict(signal: DatasetSignal) -> dict[str, Any]:
    return {
        "dataset": signal.dataset,
        "family": signal.family,
        "value": signal.value,
        "kind": signal.kind,
    }


def _signal_from_dict(data: dict[str, Any]) -> DatasetSignal:
    if not isinstance(data, dict):
        raise ValueError(
            f"dataset signal must be an object, got {type(data).__name__}"
        )
    return DatasetSignal(
        dataset=_require(data, "dataset"),
        family=_require(data, "family"),
        value=float(_require(data, "value")),
        kind=_require(data, "kind"),
    )


def _level_from_value(value: Any) -> RiskLevel:
    try:
        return RiskLevel(value)
    except ValueError as exc:
        raise ValueError(f"unknown risk level {value!r} in stored result") from exc


def _require(data: dict[str, Any], key: str) -> Any:
    if key not in data:
        raise ValueError(f"stored RiskResult is missing required key {key!r}")
    return data[key]
