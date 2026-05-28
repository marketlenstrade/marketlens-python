from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class SurvivalStrike(BaseModel):
    model_config = ConfigDict(frozen=True)

    strike: float
    raw_prob: float
    fitted_prob: float
    market_id: str


class DensityBucket(BaseModel):
    model_config = ConfigDict(frozen=True)

    lower: float | None = None
    upper: float | None = None
    prob: float
    normalized_prob: float
    market_id: str


class BarrierStrike(BaseModel):
    model_config = ConfigDict(frozen=True)

    strike: float
    direction: str
    raw_prob: float
    fitted_prob: float
    market_id: str


class Surface(BaseModel):
    model_config = ConfigDict(frozen=True)

    series_id: str
    event_id: str
    series_title: str | None = None
    surface_type: str
    underlying: str
    computed_at: int
    expiry_ms: int
    n_strikes: int
    implied_mean: float | None = None
    implied_cv: float | None = None
    implied_skew: float | None = None
    # Barrier-specific: expected peak/trough from reach/dip curves
    implied_peak: float | None = None
    implied_peak_cv: float | None = None
    implied_trough: float | None = None
    implied_trough_cv: float | None = None
    strikes: list[dict]

    def survival_strikes(self) -> list[SurvivalStrike]:
        return [SurvivalStrike.model_validate(s) for s in self.strikes]

    def density_buckets(self) -> list[DensityBucket]:
        return [DensityBucket.model_validate(s) for s in self.strikes]

    def barrier_strikes(self) -> list[BarrierStrike]:
        return [BarrierStrike.model_validate(s) for s in self.strikes]

    def typed_strikes(self) -> list[SurvivalStrike] | list[DensityBucket] | list[BarrierStrike]:
        """Parse strikes based on surface_type automatically."""
        if self.surface_type == "survival":
            return self.survival_strikes()
        if self.surface_type == "density":
            return self.density_buckets()
        if self.surface_type == "barrier":
            return self.barrier_strikes()
        return self.strikes
