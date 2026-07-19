"""Stage 4 of the plot-checker pipeline: the deterministic risk verdict.

EUDR compliance turns on a single question: was this plot deforested after the
2020-12-31 cutoff? No single dataset answers it with certainty, so the verdict
is built on *convergence of evidence* — the degree to which INDEPENDENT
deforestation datasets agree that tree cover was lost inside the plot after
2020. Agreement across independent datasets is the strong signal; a lone
dataset is a lead, not a conviction.

The verdict is a fixed rule table (never an LLM, never a probability), so it is
reproducible and re-explainable years later. ``RULESET_VERSION`` pins the exact
table that produced a given ``RiskResult.level``.

Independence is captured by ``DatasetSignal.family``: datasets that share a
sensor or lineage collapse to one family (both TMF deforestation and TMF
degradation are ``TMF``; GLAD-Landsat and GLAD-Sentinel-2 are both ``GLAD``),
so two views of the same source can never inflate "convergence".

The pipeline fails loud (see the house rules): a provider column that is
present but non-numeric raises ``RiskProviderError`` rather than being silently
dropped or coerced to zero. A missing column is simply an absent signal.
"""

from __future__ import annotations

from typing import Any

from app.geo.schemas import (
    DatasetSignal,
    PlotEvidence,
    RiskProviderError,
    RiskResult,
)
from app.models.enums import Commodity, RiskLevel

# --------------------------------------------------------------------------- #
# Ruleset constants — versioned and documented                                #
# --------------------------------------------------------------------------- #
# Bump RULESET_VERSION whenever the rule table below changes so that any stored
# verdict can be traced back to the exact logic that produced it.
RULESET_VERSION = "2026.07.1"

# A dataset signal only counts once its mapped area clears these floors. They
# guard against sub-pixel noise: a few square metres of "loss" is measurement
# jitter, not deforestation, and must not tip a plot into a worse verdict.
MIN_SIGNAL_HA = 0.01  # minimum post-2020 loss area (ha) for a loss signal to count
MIN_FOREST_HA = 0.01  # minimum forest area (ha) for baseline forest to be "present"

# Deforestation-free cutoff year (EUDR: after 2020-12-31 => non-compliant).
CUTOFF_YEAR = 2020


# --------------------------------------------------------------------------- #
# Whisp column -> (family, kind) mapping                                        #
# --------------------------------------------------------------------------- #
# Documented Whisp v2.1.0 result columns. Each maps to the independence family
# it belongs to and the kind of signal it carries. Keys are matched EXACTLY
# against the property keys of the Whisp payload, including the hyphenated GLAD
# columns ("GLAD-L_after_2020", "GLAD-S2_after_2020"). A column absent from this
# mapping is ignored; a column present here but non-numeric in the payload is a
# hard error (the provider changed its contract and we must not guess).
WHISP_COLUMN_FAMILIES: dict[str, tuple[str, str]] = {
    # Baseline forest cover at the 2020 cutoff.
    "EUFO_2020": ("EUFO", "forest_2020"),
    "GFC_TC_2020": ("GFC", "forest_2020"),
    "TMF_undist": ("TMF", "forest_2020"),
    "GLAD_Primary": ("GLAD", "forest_2020"),
    "ESA_TC_2020": ("ESA", "forest_2020"),
    "Forest_FDaP": ("FDaP", "forest_2020"),
    # Tree-cover loss AFTER the 2020 cutoff (the convergence signal).
    "GFC_loss_after_2020": ("GFC", "loss_after_2020"),
    "TMF_def_after_2020": ("TMF", "loss_after_2020"),
    "TMF_deg_after_2020": ("TMF", "loss_after_2020"),
    "RADD_after_2020": ("RADD", "loss_after_2020"),
    "GLAD-L_after_2020": ("GLAD", "loss_after_2020"),
    "GLAD-S2_after_2020": ("GLAD", "loss_after_2020"),
    "nBR_PRODES_deforestation_Brazil_after_2020": ("PRODES", "loss_after_2020"),
    "nBR_DETER_forestdegradation_Amazon_after_2020": ("DETER", "loss_after_2020"),
    # Commodity land cover at 2020 (informational context only).
    "Oil_palm_Descals": ("oil_palm", "commodity_2020"),
    "Oil_palm_FDaP": ("oil_palm", "commodity_2020"),
    "Coffee_FDaP": ("coffee", "commodity_2020"),
    "Cocoa_ETH": ("cocoa", "commodity_2020"),
    "Cocoa_FDaP": ("cocoa", "commodity_2020"),
    "Rubber_RBGE": ("rubber", "commodity_2020"),
    "Rubber_FDaP": ("rubber", "commodity_2020"),
    "Soy_Song_2020": ("soy", "commodity_2020"),
    "TMF_plant": ("plantation", "commodity_2020"),
}

# Which ``kind`` each mapped column feeds on the resulting PlotEvidence.
_KIND_TO_FIELD = {
    "forest_2020": "forest_2020",
    "loss_after_2020": "loss_after_2020",
    "commodity_2020": "commodity_2020",
}


# --------------------------------------------------------------------------- #
# Rationale helpers                                                             #
# --------------------------------------------------------------------------- #
def _cite(signals: tuple[DatasetSignal, ...]) -> str:
    """Render signals as "FAMILY (0.42 ha), OTHER (0.31 ha)" for the rationale."""
    return ", ".join(f"{s.family} ({s.value:.2f} ha)" for s in signals)


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #
def assess(evidence: PlotEvidence, *, commodity: Commodity | None = None) -> RiskResult:
    """Return the deterministic green/amber/red verdict for one plot.

    The rule table (fixed; ``RULESET_VERSION`` pins it):

    * ``converging_families`` = distinct independence families among the
      in-plot post-2020 loss signals whose area exceeds ``MIN_SIGNAL_HA``.
    * ``signals_in_plot`` = number of those families.
    * ``buffer_families`` = families with post-2020 loss in the surrounding
      buffer (over ``MIN_SIGNAL_HA``) that are NOT already converging inside the
      plot; ``signals_in_buffer`` = number of those families.
    * ``forest_2020_present`` = any baseline forest signal over ``MIN_FOREST_HA``.

    Verdict:

    * two or more converging families inside the plot -> RED (independent
      corroboration of post-2020 deforestation).
    * exactly one -> AMBER (a lead without corroboration).
    * none inside, but at least one family in the buffer -> AMBER (loss nearby).
    * otherwise -> GREEN.

    ``commodity`` is INFORMATIONAL ONLY: it appends a context line to the
    rationale but MUST NOT change ``level``. The verdict is a function of the
    deforestation evidence alone.
    """
    # In-plot post-2020 loss, grouped into distinct independence families.
    plot_loss = tuple(s for s in evidence.loss_after_2020 if s.value > MIN_SIGNAL_HA)
    converging_families = tuple(sorted({s.family for s in plot_loss}))
    signals_in_plot = len(converging_families)

    # Buffer loss only counts if it introduces a family not already inside the
    # plot; a family already converging in-plot adds nothing to the buffer tally.
    plot_family_set = set(converging_families)
    buffer_loss = tuple(
        s
        for s in evidence.loss_after_2020_buffer
        if s.value > MIN_SIGNAL_HA and s.family not in plot_family_set
    )
    buffer_families = tuple(sorted({s.family for s in buffer_loss}))
    signals_in_buffer = len(buffer_families)

    forest_signals = tuple(s for s in evidence.forest_2020 if s.value > MIN_FOREST_HA)
    forest_2020_present = len(forest_signals) > 0

    # --- Deterministic verdict + rationale ------------------------------------
    rationale: list[str] = []
    if signals_in_plot >= 2:
        level = RiskLevel.red
        cited = _cite(_first_per_family(plot_loss, converging_families))
        rationale.append(
            f"RED: post-2020 loss converges across {signals_in_plot} "
            f"independent datasets: {cited}"
        )
    elif signals_in_plot == 1:
        level = RiskLevel.amber
        cited = _cite(_first_per_family(plot_loss, converging_families))
        rationale.append(
            f"AMBER: post-2020 loss in a single dataset ({cited}); "
            "no independent corroboration"
        )
    elif signals_in_buffer >= 1:
        level = RiskLevel.amber
        cited = _cite(_first_per_family(buffer_loss, buffer_families))
        rationale.append(
            f"AMBER: post-2020 loss within the surrounding buffer only "
            f"({cited}), not inside the plot"
        )
    elif forest_2020_present:
        level = RiskLevel.green
        cited = _cite(forest_signals)
        rationale.append(
            f"GREEN: forest present at the {CUTOFF_YEAR} cutoff ({cited}) "
            "with no post-2020 loss detected in any dataset"
        )
    else:
        level = RiskLevel.green
        rationale.append(
            f"GREEN: no forest cover recorded at the {CUTOFF_YEAR} cutoff; "
            "deforestation-free by absence of baseline forest"
        )

    # Commodity is context only. Appending this line NEVER touches ``level``
    # above; the verdict is already decided by the deforestation evidence.
    if commodity is not None:
        rationale.append(f"commodity context: {commodity.value}")

    return RiskResult(
        level=level,
        ruleset_version=RULESET_VERSION,
        forest_2020_present=forest_2020_present,
        signals_in_plot=signals_in_plot,
        signals_in_buffer=signals_in_buffer,
        converging_families=converging_families,
        rationale=tuple(rationale),
        evidence=evidence,
    )


def evidence_from_whisp_properties(
    properties: dict[str, Any],
    *,
    provider: str = "whisp",
    dataset_versions: dict[str, Any] | None = None,
) -> PlotEvidence:
    """Build ``PlotEvidence`` from a Whisp result-row ``properties`` dict.

    Only columns listed in ``WHISP_COLUMN_FAMILIES`` are considered. A mapped
    column contributes a ``DatasetSignal`` only when it is present AND its value
    coerces to a float strictly greater than zero (an explicit zero is "measured
    and found absent", not a signal). A missing column is not an error.

    A mapped column that is present but does NOT coerce to a float raises
    ``RiskProviderError``: the provider changed its contract and we must fail
    loud rather than silently drop or guess the value. Whisp reports in-plot
    areas only, so there is no buffer field to populate here.
    """
    forest: list[DatasetSignal] = []
    loss: list[DatasetSignal] = []
    commodity: list[DatasetSignal] = []
    bucket = {"forest_2020": forest, "loss_after_2020": loss, "commodity_2020": commodity}

    for key, (family, kind) in WHISP_COLUMN_FAMILIES.items():
        if key not in properties:
            continue
        raw_value = properties[key]
        try:
            value = float(raw_value)
        except (TypeError, ValueError) as exc:
            raise RiskProviderError(
                f"non-numeric value for Whisp column {key!r}: {raw_value!r}"
            ) from exc
        if value <= 0:
            continue
        bucket[kind].append(
            DatasetSignal(dataset=key, family=family, value=value, kind=kind)
        )

    return PlotEvidence(
        forest_2020=tuple(forest),
        loss_after_2020=tuple(loss),
        commodity_2020=tuple(commodity),
        provider=provider,
        dataset_versions=dataset_versions or {},
        raw=dict(properties),
    )


# --------------------------------------------------------------------------- #
# Internal helpers                                                              #
# --------------------------------------------------------------------------- #
def _first_per_family(
    signals: tuple[DatasetSignal, ...], families: tuple[str, ...]
) -> tuple[DatasetSignal, ...]:
    """Pick one representative signal per family, ordered like ``families``.

    ``families`` is already sorted, so the cited signals in the rationale follow
    the same deterministic order as ``converging_families`` on the result.
    """
    by_family: dict[str, DatasetSignal] = {}
    for signal in signals:
        by_family.setdefault(signal.family, signal)
    return tuple(by_family[family] for family in families)
