"""Shared fail-loud form parsing for the server-rendered CRUD routers.

Every onboarding router (clients, suppliers, shipments) receives HTML form
fields as strings and maps them onto the ORM with the same posture: a blank
required field or an unparseable value is a 400, an empty optional field is
stored as NULL (never ``""``), and an unknown enum value names the accepted
ones in the error detail.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TypeVar

from fastapi import HTTPException

# Any of the string enums exposed through the onboarding forms.
E = TypeVar("E", bound=StrEnum)


def clean_optional(value: str | None) -> str | None:
    """Normalize an optional field: strip, and treat blank as NULL."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def require(value: str, field: str) -> str:
    """Return a stripped required field, or raise a 400 if it is blank."""
    stripped = value.strip()
    if not stripped:
        raise HTTPException(status_code=400, detail=f"{field} is required.")
    return stripped


def parse_enum(enum_cls: type[E], value: str, field: str) -> E:
    """Map a required form value to an enum, raising a 400 on an unknown value."""
    try:
        return enum_cls(value.strip())
    except ValueError as exc:
        allowed = ", ".join(member.value for member in enum_cls)
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {field} {value!r}; expected one of {allowed}.",
        ) from exc


def parse_optional_enum(
    enum_cls: type[E], value: str | None, field: str
) -> E | None:
    """Like :func:`parse_enum`, but an empty value maps to ``None``."""
    if value is None or not value.strip():
        return None
    return parse_enum(enum_cls, value, field)


def parse_optional_float(value: str | None, field: str) -> float | None:
    """Parse an optional numeric field, raising a 400 if it is not a number."""
    if value is None or not value.strip():
        return None
    try:
        return float(value.strip())
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"Invalid {field} {value!r}; expected a number."
        ) from exc


def parse_optional_country(value: str | None, field: str = "country") -> str | None:
    """Normalize an optional 2-letter ISO country code, raising a 400 if malformed."""
    if value is None or not value.strip():
        return None
    code = value.strip().upper()
    if len(code) != 2 or not code.isalpha():
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {field} {value!r}; expected a 2-letter ISO code.",
        )
    return code
