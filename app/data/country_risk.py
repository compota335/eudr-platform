"""Static, versioned country-risk-tier table for EUDR Article 29 benchmarking.

Article 29 of Regulation (EU) 2023/1115 requires the Commission to classify
countries (or parts of countries) into three risk categories: **low**,
**standard**, and **high**. The LEGAL DEFAULT is ``standard``: Article 29(3)
states that countries are standard risk unless they have been identified as low
or high risk by the Commission. Returning ``standard`` for an unlisted country
is therefore the regulation's own default, not a fabricated guess.

The tier drives how deep the due diligence goes:

* **low** — simplified due diligence is permitted (Article 13): the operator
  still collects information but the formal risk assessment (Article 10) and
  mitigation (Article 11) are waived.
* **standard** — full due diligence: information collection, risk assessment,
  and mitigation where risk is not negligible.
* **high** — full due diligence plus enhanced scrutiny; competent authorities
  also apply enhanced checks to high-risk-origin volumes.

Fidelity policy (house rule: FAIL LOUD, NEVER FAKE SUCCESS):

The project's domain notes flag the concrete country benchmarking as UNVERIFIED
(deep dive Section 9, item 5; business plan "benchmarking de riesgo país ... sin
verificar"). The Commission's benchmarking list is short and is amended by
implementing act; a stale or invented classification here would be exactly the
fabricated regulatory data the house rules forbid. We therefore:

* keep the explicit ``HIGH_RISK`` and ``LOW_RISK`` sets MINIMAL and conservative,
  populated only from what is confidently established, and
* rely on the ``standard`` legal default for everything else.

Both sets are intentionally allowed to be empty. An empty set means "no country
is confidently classified into this tier in this table version" and makes every
valid, unlisted country resolve to the ``standard`` default — the safe, legally
correct behaviour. Populate these sets ONLY from the published Article 29
implementing act, and bump :data:`COUNTRY_RISK_VERSION` when you do.
"""

from __future__ import annotations

from app.models.enums import CountryRiskTier

# Bump whenever the classification sets below change so any stored due-diligence
# decision can be traced back to the exact benchmarking snapshot that produced
# it. The suffix marks that the concrete lists are conservative pending
# verification of the Article 29 implementing act.
COUNTRY_RISK_VERSION = "art29.2023.1115.conservative-unverified"


# --------------------------------------------------------------------------- #
# Explicit classifications — ISO 3166-1 alpha-2, UPPERCASE                      #
# --------------------------------------------------------------------------- #
# Deliberately empty pending verification of the Commission's Article 29
# benchmarking implementing act (see module docstring / deep dive Section 9).
# Do NOT populate with guesses: an unlisted country correctly falls through to
# the ``standard`` legal default below. When the published list is confirmed,
# add its ISO alpha-2 members here and bump COUNTRY_RISK_VERSION.
HIGH_RISK: frozenset[str] = frozenset()
LOW_RISK: frozenset[str] = frozenset()


def _normalize_country(country_code: str) -> str:
    """Normalize an ISO 3166-1 alpha-2 code to uppercase, or fail loud.

    A valid code is exactly two ASCII letters (e.g. "de", "De", "DE" -> "DE").
    Anything else (wrong length, digits, a country name) is a caller error and
    raises ``ValueError`` rather than being coerced. This does NOT verify the
    code is an assigned country; it only enforces the syntactic shape.
    """
    normalized = country_code.strip().upper()
    if len(normalized) != 2 or not normalized.isascii() or not normalized.isalpha():
        raise ValueError(
            f"invalid ISO 3166-1 alpha-2 country code: {country_code!r} "
            "(expected two ASCII letters)"
        )
    return normalized


def risk_tier_for_country(country_code: str) -> CountryRiskTier:
    """Return the Article 29 risk tier for an ISO 3166-1 alpha-2 country code.

    Normalizes the code to uppercase and looks it up against the explicit
    high/low sets. A syntactically INVALID code (not two ASCII letters) raises
    ``ValueError`` — fail loud, do not guess. A syntactically valid but unlisted
    code returns :attr:`CountryRiskTier.standard`, which is the Article 29 legal
    default (Article 29(3)), not a fabricated classification.
    """
    normalized = _normalize_country(country_code)
    if normalized in HIGH_RISK:
        return CountryRiskTier.high
    if normalized in LOW_RISK:
        return CountryRiskTier.low
    return CountryRiskTier.standard
