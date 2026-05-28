"""Implied probability surfaces — survival, density, and barrier.

Pre-computed every 5 minutes from live L2 midpoints via isotonic regression.
"""

from marketlens import MarketLens

client = MarketLens()

for surface in client.signals.surfaces(underlying="BTC"):
    print(f"\n{surface.series_title}  [{surface.surface_type}]")

    if surface.surface_type == "survival":
        for s in surface.survival_strikes():
            flag = " *" if abs(s.fitted_prob - s.raw_prob) > 0.01 else ""
            print(f"  K=${s.strike:>10,.0f}  P(above)={s.fitted_prob:.3f}  raw={s.raw_prob:.3f}{flag}")
        print(f"  → mean=${surface.implied_mean:,.0f}  cv={surface.implied_cv}%")

    elif surface.surface_type == "density":
        for b in surface.density_buckets():
            lo = f"${b.lower:,.0f}" if b.lower else "<tail"
            hi = f"${b.upper:,.0f}" if b.upper else "tail>"
            print(f"  {lo:>10}-{hi:<10}  p={b.normalized_prob:.3f}")
        print(f"  → mean=${surface.implied_mean:,.0f}  cv={surface.implied_cv}%")

    elif surface.surface_type == "barrier":
        for b in surface.barrier_strikes():
            print(f"  {b.direction:>5} ${b.strike:>10,.0f}  P={b.fitted_prob:.3f}  raw={b.raw_prob:.3f}")

client.close()
