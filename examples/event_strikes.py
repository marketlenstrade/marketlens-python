"""Structured product — snapshot the implied surface at the end of each event.

Walks a multi-strike series and captures the final surface state per event,
showing how the implied distribution shifts across successive expirations.
"""

from datetime import datetime, timezone

from marketlens import MarketLens

client = MarketLens()

walk = client.orderbook.walk(
    "ethereum-multi-strikes-weekly",
    after=datetime(2026, 3, 5, 10, 0, tzinfo=timezone.utc),
    before=datetime(2026, 3, 5, 10, 5, tzinfo=timezone.utc),
)

last_event_id = None
last_surface = None

for market, book in walk:
    event = walk.event
    if event and last_event_id and event.id != last_event_id and last_surface:
        # Event boundary — print summary of the previous event
        print(f"\n{last_surface.series_title}  [{last_event_title}]")
        print(f"  implied mean: ${last_surface.implied_mean:,.0f}  cv={last_surface.implied_cv}%")
        for s in last_surface.survival_strikes():
            print(f"  K=${s.strike:>10,.0f}  P(above)={s.fitted_prob:.3f}")

    last_event_id = event.id if event else None
    last_event_title = event.title if event else None
    last_surface = walk.surface()

# Final event
if last_surface:
    print(f"\n{last_surface.series_title}  [{last_event_title}]")
    print(f"  implied mean: ${last_surface.implied_mean:,.0f}  cv={last_surface.implied_cv}%")
    for s in last_surface.survival_strikes():
        print(f"  K=${s.strike:>10,.0f}  P(above)={s.fitted_prob:.3f}")

client.close()
