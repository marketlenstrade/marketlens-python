from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from marketlens.types._validators import none_to_half, none_to_zero


class Outcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    index: int
    platform_token_id: str
    last_price: float = 0.5

    _coerce = none_to_half("last_price")


class Market(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    platform: str
    platform_market_id: str
    event_id: str
    event_title: str | None = None
    category: str | None = None
    series_id: str | None = None
    series_title: str | None = None
    series_recurrence: str | None = None
    question: str
    market_type: str
    status: str
    outcomes: list[Outcome]
    winning_outcome: str | None = None
    winning_outcome_index: int | None = None
    tick_size: float
    volume: float = 0.0
    liquidity: float = 0.0
    open_time: int | None = None
    close_time: int | None = None
    resolved_at: int | None = None
    platform_resolved_at: int | None = None
    strike: float | None = None
    strike_upper: float | None = None
    strike_direction: str | None = None
    subtype: str | None = None
    underlying: str | None = None
    # "streamed" = full snapshot + delta chain; "polled" = periodic REST
    # snapshots only (no deltas); None = collected before tiers existed
    # (streamed fidelity).
    collection_tier: str | None = None
    # When the predicted real-world event happens (ms epoch), e.g. game
    # kickoff. Sports/esports only; the platform populates it after listing.
    game_start_time: int | None = None
    # Platform bet-type slug for sports/esports markets ("moneyline",
    # "spreads", "totals", "tennis_match_totals", ... 70+ values).
    sports_market_type: str | None = None
    # Spread or total line for sports bets (e.g. -6.5).
    line: float | None = None
    # Part of a negative-risk group (mutually exclusive outcomes of one event).
    neg_risk: bool | None = None
    # Short label of this market within its event, e.g. the candidate name.
    group_item_title: str | None = None
    # The span in which the market was live in our data (ms epoch): the
    # first non-empty book (not before open_time) to the platform's
    # resolution or the last non-empty book; data_end is None while the
    # market is still open. Both None against servers that predate them.
    data_start: int | None = None
    data_end: int | None = None
    created_at: int
    updated_at: int

    _coerce = none_to_zero("volume", "liquidity")
