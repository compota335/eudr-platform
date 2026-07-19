"""Stage 1 of the pipeline: deterministic EUDR scope determination.

Whether a product falls under Regulation (EU) 2023/1115 is decided by the
**Combined Nomenclature (CN) code** declared to customs, matched against Annex I
(see :mod:`app.data.cn_codes`). This module makes that determination and, when
an origin country is supplied, attaches the Article 29 country-risk tier (see
:mod:`app.data.country_risk`) with its due-diligence implication.

Determinism policy (house rule: LLM for language, deterministic code for scope):

* The **CN code is the sole authority** for ``in_scope``. If a CN code is given,
  the Annex I table decides scope and commodity. Nothing else can assert
  authoritative scope.
* Free text NEVER decides scope. When no CN code is given but a product
  description is, a fixed keyword dictionary (NOT an LLM) may SUGGEST a candidate
  commodity to guide the operator toward the right CN heading. A suggestion is
  explicitly marked in the rationale as requiring CN confirmation and leaves
  ``in_scope`` as ``False`` with ``cn_code`` unset: it is a lead, not a verdict.
* If neither a CN code nor a description is provided there is nothing to check,
  so the call fails loud with :class:`ScopeError`.

Everything here is pure and deterministic: same inputs, same
:class:`ScopeResult`, no I/O, no model calls. The result carries both table
version strings so any stored scope decision is reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.data.cn_codes import (
    CN_CODE_TABLE_VERSION,
    commodity_for_cn,
    matched_heading_for_cn,
)
from app.data.country_risk import COUNTRY_RISK_VERSION, risk_tier_for_country
from app.models.enums import Commodity, CountryRiskTier


# --------------------------------------------------------------------------- #
# Errors — scope determination fails loud, never guesses.                      #
# --------------------------------------------------------------------------- #
class ScopeError(ValueError):
    """Base class for scope-determination input errors."""


# --------------------------------------------------------------------------- #
# Standard EUDR Due Diligence Statement (DDS) documentation requirements.       #
# --------------------------------------------------------------------------- #
# These are the core evidence items every in-scope commodity needs for a DDS,
# independent of which commodity it is (Regulation (EU) 2023/1115, Articles 9
# and 10). Populated on the result whenever a product is confirmed in scope.
REQUIRED_DOCUMENTATION: tuple[str, ...] = (
    "Geolocation coordinates of all plots of land where the commodity was "
    "produced (polygon for plots over 4 ha; a point is allowed for smaller "
    "plots), at 6-decimal precision.",
    "Evidence that the plots are deforestation-free: no deforestation after the "
    "2020-12-31 cutoff (and, for wood, no forest degradation).",
    "Documentation that the commodity was produced legally under the laws of the "
    "country of production (land-use rights, environmental, labour, human rights, "
    "tax, anti-corruption and trade/customs law).",
)


# --------------------------------------------------------------------------- #
# Keyword suggestion dictionary (deterministic, NOT an LLM).                    #
# --------------------------------------------------------------------------- #
# Maps lowercase whole-word keywords to a candidate commodity. Used only to
# SUGGEST a commodity from a free-text description when no CN code is given; the
# suggestion never sets ``in_scope``. Keywords are matched as whole,
# lowercase tokens so "coffee" matches but "coffeehouse" does not.
_KEYWORD_TO_COMMODITY: dict[str, Commodity] = {
    # cattle
    "cattle": Commodity.cattle,
    "bovine": Commodity.cattle,
    "beef": Commodity.cattle,
    "leather": Commodity.cattle,
    "hide": Commodity.cattle,
    "hides": Commodity.cattle,
    # cocoa
    "cocoa": Commodity.cocoa,
    "cacao": Commodity.cocoa,
    "chocolate": Commodity.cocoa,
    # coffee
    "coffee": Commodity.coffee,
    # oil palm
    "palm": Commodity.oil_palm,
    "palm-oil": Commodity.oil_palm,
    # rubber
    "rubber": Commodity.rubber,
    "latex": Commodity.rubber,
    "tyre": Commodity.rubber,
    "tyres": Commodity.rubber,
    "tire": Commodity.rubber,
    "tires": Commodity.rubber,
    # soya
    "soya": Commodity.soya,
    "soy": Commodity.soya,
    "soybean": Commodity.soya,
    "soybeans": Commodity.soya,
    # wood
    "wood": Commodity.wood,
    "timber": Commodity.wood,
    "lumber": Commodity.wood,
    "plywood": Commodity.wood,
    "furniture": Commodity.wood,
    "paper": Commodity.wood,
    "pulp": Commodity.wood,
    "charcoal": Commodity.wood,
}


# --------------------------------------------------------------------------- #
# Result                                                                        #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ScopeResult:
    """The deterministic EUDR scope verdict for one product line.

    ``in_scope`` is authoritative ONLY when it was decided from ``cn_code``. A
    description-only keyword suggestion leaves ``in_scope`` ``False`` and records
    the candidate in ``commodity`` with a rationale line flagging that a CN code
    is required to confirm scope.

    The two ``*_version`` fields pin the exact Annex I and Article 29 tables that
    produced this verdict so it can be reproduced and re-explained later.
    """

    in_scope: bool
    commodity: Commodity | None
    cn_code: str | None  # normalized declared code (digits only), if one was given
    matched_cn: str | None  # the Annex I heading it matched, if in scope
    country_code: str | None  # normalized ISO 3166-1 alpha-2, if one was given
    country_risk: CountryRiskTier | None
    rationale: tuple[str, ...]
    required_documentation: tuple[str, ...]
    cn_table_version: str
    country_table_version: str


# --------------------------------------------------------------------------- #
# Internal helpers                                                             #
# --------------------------------------------------------------------------- #
def _suggest_commodity(description: str) -> tuple[Commodity | None, str | None]:
    """Suggest a candidate commodity from free text via the keyword dictionary.

    Returns ``(commodity, matched_keyword)`` for the first keyword found as a
    whole lowercase token, or ``(None, None)`` when nothing matches. Purely
    lexical: no model, no scope decision.
    """
    tokens = {token.strip(".,;:()[]{}\"'") for token in description.lower().split()}
    for keyword, commodity in _KEYWORD_TO_COMMODITY.items():
        if keyword in tokens:
            return commodity, keyword
    return None, None


def _country_rationale(tier: CountryRiskTier) -> str:
    """One rationale line explaining the due-diligence depth for a country tier."""
    if tier is CountryRiskTier.high:
        return (
            "Origin country is HIGH risk (Article 29): full due diligence plus "
            "enhanced scrutiny of the risk assessment and mitigation is required."
        )
    if tier is CountryRiskTier.low:
        return (
            "Origin country is LOW risk (Article 29): simplified due diligence is "
            "permitted under Article 13 (information collection required; formal "
            "risk assessment and mitigation waived)."
        )
    return (
        "Origin country is STANDARD risk (Article 29 legal default): full due "
        "diligence is required (information collection, risk assessment, and "
        "mitigation where risk is not negligible)."
    )


# --------------------------------------------------------------------------- #
# Public API                                                                    #
# --------------------------------------------------------------------------- #
def check_scope(
    *,
    product_description: str | None = None,
    cn_code: str | None = None,
    origin_country: str | None = None,
) -> ScopeResult:
    """Determine whether a product line falls under EUDR, deterministically.

    The CN code decides scope; free text can only suggest a candidate commodity
    (see the module docstring for the full policy).

    Args:
        product_description: Free-text product description. Optional. Used only
            to suggest a candidate commodity when no CN code is given; never
            decides scope on its own.
        cn_code: The Combined Nomenclature code declared to customs. When given,
            the Annex I table decides ``in_scope`` and ``commodity``.
        origin_country: ISO 3166-1 alpha-2 code of the country of production.
            When given, the Article 29 risk tier and its due-diligence
            implication are attached to the result.

    Returns:
        A :class:`ScopeResult`.

    Raises:
        ScopeError: if neither ``cn_code`` nor ``product_description`` is
            provided (nothing to check), or if ``cn_code`` is malformed.
        ScopeError: if ``origin_country`` is not a syntactically valid ISO
            3166-1 alpha-2 code.
    """
    description = product_description.strip() if product_description else None
    raw_cn = cn_code.strip() if cn_code else None

    if not raw_cn and not description:
        raise ScopeError(
            "nothing to check: provide a cn_code or a product_description"
        )

    rationale: list[str] = []

    # --- Country risk (independent of scope; attach whenever a country is given).
    country_code: str | None = None
    country_risk: CountryRiskTier | None = None
    if origin_country and origin_country.strip():
        try:
            country_risk = risk_tier_for_country(origin_country)
        except ValueError as exc:
            raise ScopeError(str(exc)) from exc
        country_code = origin_country.strip().upper()

    # --- Scope decision: CN code is authoritative. -----------------------------
    in_scope = False
    commodity: Commodity | None = None
    normalized_cn: str | None = None
    matched_cn: str | None = None
    required_documentation: tuple[str, ...] = ()

    if raw_cn:
        try:
            commodity = commodity_for_cn(raw_cn)
            matched_cn = matched_heading_for_cn(raw_cn)
        except ValueError as exc:
            raise ScopeError(f"invalid CN code: {exc}") from exc
        # Preserve the caller's declared code, normalized to digits only.
        normalized_cn = raw_cn.replace(".", "").replace(" ", "")
        if commodity is not None and matched_cn is not None:
            in_scope = True
            required_documentation = REQUIRED_DOCUMENTATION
            rationale.append(
                f"IN SCOPE: CN code {normalized_cn} matches Annex I heading "
                f"{matched_cn} for {commodity.value} under Regulation (EU) "
                "2023/1115."
            )
        else:
            rationale.append(
                f"OUT OF SCOPE: CN code {normalized_cn} does not match any Annex I "
                "heading of Regulation (EU) 2023/1115."
            )
    elif description is not None:
        # Description-only path: keyword suggestion, never an authoritative scope.
        suggested, keyword = _suggest_commodity(description)
        if suggested is not None:
            commodity = suggested
            rationale.append(
                f"SUGGESTION ONLY (not authoritative): the description matches the "
                f"keyword '{keyword}', suggesting {suggested.value}. Scope is NOT "
                "confirmed: provide the CN code declared to customs so the Annex I "
                "table can decide in_scope."
            )
        else:
            rationale.append(
                "UNDETERMINED: no CN code was provided and the description matched "
                "no in-scope commodity keyword. Provide the CN code to decide scope."
            )

    # --- Country rationale, appended after the scope line(s). -------------------
    if country_risk is not None:
        rationale.append(_country_rationale(country_risk))

    return ScopeResult(
        in_scope=in_scope,
        commodity=commodity,
        cn_code=normalized_cn,
        matched_cn=matched_cn,
        country_code=country_code,
        country_risk=country_risk,
        rationale=tuple(rationale),
        required_documentation=required_documentation,
        cn_table_version=CN_CODE_TABLE_VERSION,
        country_table_version=COUNTRY_RISK_VERSION,
    )
