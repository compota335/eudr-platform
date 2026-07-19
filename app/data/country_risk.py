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

The Commission's benchmarking list is short and is amended by implementing act;
a stale or invented classification here would be exactly the fabricated
regulatory data the house rules forbid. So each tier is populated ONLY from the
published act, member by member:

* ``HIGH_RISK`` carries the four countries named high risk in the first
  benchmarking act, Implementing Regulation (EU) 2025/1093 (22 May 2025).
* ``LOW_RISK`` is intentionally left EMPTY until that act's ~140-country Annex
  is transcribed in full from the official text (not from a summary).

An unlisted country resolves to ``standard``, the Article 29(3) legal default.
That default is also the SAFE direction: treating a truly-low-risk country as
standard only OVER-applies due diligence, it never under-applies it. Populate a
tier ONLY from the published implementing act and bump
:data:`COUNTRY_RISK_VERSION` when you do.
"""

from __future__ import annotations

from app.models.enums import CountryRiskTier

# Bump whenever the classification sets below change so any stored due-diligence
# decision can be traced back to the exact benchmarking snapshot that produced
# it. This snapshot carries the four HIGH-risk countries from the first
# benchmarking act, Implementing Regulation (EU) 2025/1093; the LOW set is still
# pending transcription of that act's ~140-country Annex (see below).
COUNTRY_RISK_VERSION = "art29.ir2025-1093.high-verified.low-pending"


# --------------------------------------------------------------------------- #
# Explicit classifications — ISO 3166-1 alpha-2, UPPERCASE                      #
# --------------------------------------------------------------------------- #
# HIGH: the only four countries the Commission classified high risk in the first
# benchmarking act, Implementing Regulation (EU) 2025/1093 of 22 May 2025 —
# Belarus (BY), North Korea / DPRK (KP), Myanmar (MM), Russia (RU). These are
# whole-country classifications, driven chiefly by UN/EU sanctions on the covered
# goods. Any country outside this set falls through to the STANDARD default below.
HIGH_RISK: frozenset[str] = frozenset({"BY", "KP", "MM", "RU"})

# LOW: the same act classified ~140 countries low risk (all EU Member States, the
# UK, US, Canada, China, Japan, Australia, South Africa, ...), which unlocks
# simplified due diligence (Article 13). Left EMPTY until the full ISO-code Annex
# is transcribed from the official act: an unlisted country resolves to STANDARD,
# which only ever OVER-applies due diligence (the safe direction). Do NOT fill
# this from a summary — only from the published Annex — and bump the version.
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
