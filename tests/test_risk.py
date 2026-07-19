"""Tests for the deterministic risk verdict (Stage 4, convergence of evidence)."""

from __future__ import annotations

import pytest

from app.geo.schemas import DatasetSignal, PlotEvidence, RiskProviderError
from app.models.enums import Commodity, RiskLevel
from app.services.risk import (
    RULESET_VERSION,
    assess,
    evidence_from_whisp_properties,
)


def _loss(family: str, value: float, dataset: str | None = None) -> DatasetSignal:
    return DatasetSignal(
        dataset=dataset or f"{family}_loss_after_2020",
        family=family,
        value=value,
        kind="loss_after_2020",
    )


def _forest(family: str, value: float) -> DatasetSignal:
    return DatasetSignal(
        dataset=f"{family}_2020", family=family, value=value, kind="forest_2020"
    )


# --------------------------------------------------------------------------- #
# Verdict rule table                                                           #
# --------------------------------------------------------------------------- #
def test_two_plot_families_is_red() -> None:
    evidence = PlotEvidence(
        loss_after_2020=(_loss("GFC", 0.42), _loss("RADD", 0.31)),
    )
    result = assess(evidence)
    assert result.level is RiskLevel.red
    assert result.signals_in_plot == 2
    assert result.converging_families == ("GFC", "RADD")
    # Rationale cites both datasets with their ha values.
    assert "GFC (0.42 ha)" in result.rationale[0]
    assert "RADD (0.31 ha)" in result.rationale[0]


def test_single_plot_family_is_amber() -> None:
    evidence = PlotEvidence(loss_after_2020=(_loss("GFC", 0.42),))
    result = assess(evidence)
    assert result.level is RiskLevel.amber
    assert result.signals_in_plot == 1
    assert result.converging_families == ("GFC",)
    assert "single dataset" in result.rationale[0]
    assert "GFC (0.42 ha)" in result.rationale[0]


def test_buffer_only_is_amber() -> None:
    evidence = PlotEvidence(
        loss_after_2020_buffer=(_loss("RADD", 0.10),),
    )
    result = assess(evidence)
    assert result.level is RiskLevel.amber
    assert result.signals_in_plot == 0
    assert result.signals_in_buffer == 1
    assert "buffer only" in result.rationale[0]
    assert "RADD (0.10 ha)" in result.rationale[0]


def test_green_with_forest_mentions_forest() -> None:
    evidence = PlotEvidence(forest_2020=(_forest("EUFO", 0.90),))
    result = assess(evidence)
    assert result.level is RiskLevel.green
    assert result.forest_2020_present is True
    assert "forest present" in result.rationale[0]
    assert "EUFO (0.90 ha)" in result.rationale[0]


def test_green_without_forest_mentions_absence_of_baseline() -> None:
    result = assess(PlotEvidence())
    assert result.level is RiskLevel.green
    assert result.forest_2020_present is False
    assert result.signals_in_plot == 0
    assert result.signals_in_buffer == 0
    assert "absence of baseline forest" in result.rationale[0]


def test_same_family_twice_counts_once() -> None:
    # TMF deforestation and TMF degradation are the SAME independence family:
    # two views of one source must not be read as convergence.
    evidence = PlotEvidence(
        loss_after_2020=(
            _loss("TMF", 0.20, dataset="TMF_def_after_2020"),
            _loss("TMF", 0.15, dataset="TMF_deg_after_2020"),
        ),
    )
    result = assess(evidence)
    assert result.level is RiskLevel.amber
    assert result.signals_in_plot == 1
    assert result.converging_families == ("TMF",)


def test_signals_below_floor_do_not_count() -> None:
    # Sub-pixel noise (<= MIN_SIGNAL_HA) is not deforestation.
    evidence = PlotEvidence(
        loss_after_2020=(_loss("GFC", 0.005), _loss("RADD", 0.31)),
    )
    result = assess(evidence)
    assert result.level is RiskLevel.amber
    assert result.converging_families == ("RADD",)


def test_buffer_family_already_in_plot_is_not_double_counted() -> None:
    # A family converging in-plot must not also inflate the buffer tally.
    evidence = PlotEvidence(
        loss_after_2020=(_loss("GFC", 0.30),),
        loss_after_2020_buffer=(_loss("GFC", 0.50),),
    )
    result = assess(evidence)
    assert result.level is RiskLevel.amber
    assert result.signals_in_plot == 1
    assert result.signals_in_buffer == 0


# --------------------------------------------------------------------------- #
# Whisp property mapping                                                        #
# --------------------------------------------------------------------------- #
def test_evidence_from_whisp_then_assess_is_red() -> None:
    properties = {
        "GFC_loss_after_2020": 0.4,
        "RADD_after_2020": 0.3,
        "EUFO_2020": 0.9,
    }
    evidence = evidence_from_whisp_properties(properties)
    assert evidence.provider == "whisp"
    assert evidence.raw == properties
    result = assess(evidence)
    assert result.level is RiskLevel.red
    assert "GFC" in result.converging_families
    assert "RADD" in result.converging_families


def test_evidence_from_whisp_ignores_missing_and_zero() -> None:
    # Missing keys are not errors; an explicit zero is "measured absent".
    evidence = evidence_from_whisp_properties(
        {"GFC_loss_after_2020": 0.0, "EUFO_2020": 0.9}
    )
    assert evidence.loss_after_2020 == ()
    assert len(evidence.forest_2020) == 1


def test_evidence_from_whisp_hyphenated_glad_key() -> None:
    # The hyphenated GLAD columns are matched as exact string keys.
    evidence = evidence_from_whisp_properties(
        {"GLAD-L_after_2020": 0.2, "GLAD-S2_after_2020": 0.3}
    )
    # Both collapse to the GLAD family: one converging family, not two.
    assert {s.family for s in evidence.loss_after_2020} == {"GLAD"}
    assert assess(evidence).level is RiskLevel.amber


def test_evidence_from_whisp_non_numeric_raises() -> None:
    with pytest.raises(RiskProviderError, match="non-numeric value for Whisp column"):
        evidence_from_whisp_properties({"GFC_loss_after_2020": "n/a"})


# --------------------------------------------------------------------------- #
# Invariants                                                                    #
# --------------------------------------------------------------------------- #
def test_result_carries_ruleset_version() -> None:
    result = assess(PlotEvidence())
    assert result.ruleset_version == RULESET_VERSION


def test_commodity_does_not_change_level() -> None:
    # Commodity is informational: same evidence, same verdict, extra context.
    evidence = PlotEvidence(loss_after_2020=(_loss("GFC", 0.42),))
    without = assess(evidence)
    with_commodity = assess(evidence, commodity=Commodity.coffee)
    assert with_commodity.level is without.level
    assert with_commodity.rationale[-1] == "commodity context: coffee"
    # The added line is context only, appended after the verdict line.
    assert without.rationale[0] == with_commodity.rationale[0]
