from __future__ import annotations

from pydantic import field_validator


def none_to_zero(*fields: str):
    """Coerce None to 0.0 for numeric fields where missing data should
    behave as zero (sizes, depths, fees, volumes) so callers don't need
    to None-check."""

    @field_validator(*fields, mode="before")
    @classmethod
    def _coerce(cls, v):
        return 0.0 if v is None else v

    return _coerce


def none_to_half(*fields: str):
    """Coerce None to 0.5 for Polymarket price fields where missing data
    should behave as the neutral [0, 1] prior — empty book, never-traded
    outcome. Lets ``if book.midpoint < 0.4`` work without an explicit
    ``is None`` check on either side of the threshold."""

    @field_validator(*fields, mode="before")
    @classmethod
    def _coerce(cls, v):
        return 0.5 if v is None else v

    return _coerce
