"""Tests for Stage 1: deterministic EUDR scope determination."""

from __future__ import annotations

import pytest

from app.data import country_risk
from app.data.cn_codes import (
    CN_CODE_TABLE_VERSION,
    commodity_for_cn,
    matched_heading_for_cn,
)
from app.data.country_risk import (
    COUNTRY_RISK_VERSION,
    risk_tier_for_country,
)
from app.models.enums import Commodity, CountryRiskTier
from app.services.scope import ScopeError, check_scope


# --------------------------------------------------------------------------- #
# CN code table -> commodity                                                   #
# --------------------------------------------------------------------------- #
def test_cn_heading_in_scope_is_coffee() -> None:
    assert commodity_for_cn("0901") is Commodity.coffee
    assert matched_heading_for_cn("0901") == "0901"


def test_cn_subheading_prefix_matches_heading() -> None:
    # A more specific declared code resolves via its Annex I heading.
    assert commodity_for_cn("090121") is Commodity.coffee
    assert matched_heading_for_cn("090121") == "0901"


def test_cn_normalizes_dots_and_spaces() -> None:
    assert commodity_for_cn("0901.21") is Commodity.coffee
    assert commodity_for_cn("1511 90") is Commodity.oil_palm


def test_cn_out_of_scope_returns_none() -> None:
    # 8471 = automatic data-processing machines (computers): not in Annex I.
    assert commodity_for_cn("8471") is None
    assert matched_heading_for_cn("8471") is None


def test_cn_more_specific_subheading_wins() -> None:
    # "120810" (soya flour) is a table subheading; it must resolve to soya even
    # though the shorter "1201" soya heading also exists. Both are soya here, so
    # assert the specific heading is the one reported as matched.
    assert commodity_for_cn("120810") is Commodity.soya
    assert matched_heading_for_cn("120810") == "120810"


def test_cn_invalid_code_raises() -> None:
    with pytest.raises(ValueError):
        commodity_for_cn("not-a-code")
    with pytest.raises(ValueError):
        commodity_for_cn("")


# --------------------------------------------------------------------------- #
# Country risk tiers (Article 29)                                              #
# --------------------------------------------------------------------------- #
def test_unlisted_country_defaults_to_standard() -> None:
    # Germany is not on the (conservative, empty) high/low lists -> standard,
    # the Article 29 legal default.
    assert risk_tier_for_country("DE") is CountryRiskTier.standard


def test_country_code_normalizes_case() -> None:
    assert risk_tier_for_country("de") is CountryRiskTier.standard
    assert risk_tier_for_country(" Br ") is CountryRiskTier.standard


def test_high_and_low_list_members_map_correctly(monkeypatch: pytest.MonkeyPatch) -> None:
    # The published Article 29 lists are intentionally empty pending
    # verification, so exercise the mechanism with patched sets: a member of the
    # high set maps to high, a member of the low set maps to low.
    monkeypatch.setattr(country_risk, "HIGH_RISK", frozenset({"AA"}))
    monkeypatch.setattr(country_risk, "LOW_RISK", frozenset({"BB"}))
    assert risk_tier_for_country("aa") is CountryRiskTier.high
    assert risk_tier_for_country("bb") is CountryRiskTier.low
    # Anything still unlisted remains the standard default.
    assert risk_tier_for_country("CC") is CountryRiskTier.standard


def test_invalid_country_code_raises() -> None:
    with pytest.raises(ValueError):
        risk_tier_for_country("XX1")  # three characters
    with pytest.raises(ValueError):
        risk_tier_for_country("Germany")  # a name, not a code
    with pytest.raises(ValueError):
        risk_tier_for_country("D")  # one letter


# --------------------------------------------------------------------------- #
# check_scope: CN decides scope                                                #
# --------------------------------------------------------------------------- #
def test_check_scope_cn_in_scope() -> None:
    result = check_scope(cn_code="0901")
    assert result.in_scope is True
    assert result.commodity is Commodity.coffee
    assert result.cn_code == "0901"
    assert result.matched_cn == "0901"
    assert result.required_documentation  # DDS hints populated when in scope
    assert "IN SCOPE" in result.rationale[0]


def test_check_scope_cn_subheading_in_scope() -> None:
    result = check_scope(cn_code="090121")
    assert result.in_scope is True
    assert result.commodity is Commodity.coffee
    assert result.matched_cn == "0901"


def test_check_scope_cn_out_of_scope() -> None:
    result = check_scope(cn_code="8471")
    assert result.in_scope is False
    assert result.commodity is None
    assert result.matched_cn is None
    assert result.cn_code == "8471"
    assert result.required_documentation == ()
    assert "OUT OF SCOPE" in result.rationale[0]


def test_check_scope_invalid_cn_raises_scope_error() -> None:
    with pytest.raises(ScopeError):
        check_scope(cn_code="abcd")


# --------------------------------------------------------------------------- #
# check_scope: description-only keyword suggestion path                        #
# --------------------------------------------------------------------------- #
def test_check_scope_description_only_suggests_not_authoritative() -> None:
    result = check_scope(product_description="Roasted arabica coffee beans")
    # Keyword suggests coffee, but scope is NOT asserted without a CN code.
    assert result.commodity is Commodity.coffee
    assert result.in_scope is False
    assert result.cn_code is None
    assert result.matched_cn is None
    assert result.required_documentation == ()
    assert any("SUGGESTION ONLY" in line for line in result.rationale)
    assert any("provide the cn code" in line.lower() for line in result.rationale)


def test_check_scope_description_no_keyword_is_undetermined() -> None:
    result = check_scope(product_description="stainless steel bolts")
    assert result.in_scope is False
    assert result.commodity is None
    assert any("UNDETERMINED" in line for line in result.rationale)


def test_check_scope_keyword_matches_whole_token_only() -> None:
    # "coffeehouse" is not the keyword "coffee": no false suggestion.
    result = check_scope(product_description="a cozy coffeehouse chain")
    assert result.commodity is None


# --------------------------------------------------------------------------- #
# check_scope: origin country attachment                                       #
# --------------------------------------------------------------------------- #
def test_check_scope_attaches_country_risk() -> None:
    result = check_scope(cn_code="0901", origin_country="br")
    assert result.country_code == "BR"
    assert result.country_risk is CountryRiskTier.standard
    assert any("STANDARD risk" in line for line in result.rationale)


def test_check_scope_invalid_country_raises_scope_error() -> None:
    with pytest.raises(ScopeError):
        check_scope(cn_code="0901", origin_country="Germany")


def test_check_scope_country_without_scope_inputs_still_needs_something() -> None:
    # A country alone is not something to scope-check: still fails loud.
    with pytest.raises(ScopeError):
        check_scope(origin_country="BR")


# --------------------------------------------------------------------------- #
# check_scope: fail-loud on empty input                                        #
# --------------------------------------------------------------------------- #
def test_check_scope_neither_cn_nor_description_raises() -> None:
    with pytest.raises(ScopeError):
        check_scope()


def test_check_scope_blank_strings_raise() -> None:
    with pytest.raises(ScopeError):
        check_scope(product_description="   ", cn_code="  ")


# --------------------------------------------------------------------------- #
# Invariants                                                                    #
# --------------------------------------------------------------------------- #
def test_result_carries_table_versions() -> None:
    result = check_scope(cn_code="0901")
    assert result.cn_table_version == CN_CODE_TABLE_VERSION
    assert result.country_table_version == COUNTRY_RISK_VERSION
