"""Microstructure feature matrix — does order book imbalance predict outcome?

Walks a rolling series, builds a per-market feature matrix from L2 replay,
and checks whether imbalance direction correlates with the winning outcome.
"""

from datetime import datetime, timedelta, timezone

from marketlens import MarketLens

client = MarketLens()
end = datetime.now(timezone.utc) - timedelta(days=1)

df = client.orderbook.walk(
    "eth-up-or-down-15m",
    after=end - timedelta(minutes=5), before=end,
).to_dataframe()

features = df.groupby("market_id").agg(
    outcome=("winning_outcome", "first"),
    imbalance=("imbalance", "mean"),
    spread_bps=("spread_bps", "mean"),
    mid_start=("midpoint", "first"),
    mid_end=("midpoint", "last"),
)
features["won_up"] = features["outcome"] == "Up"

bullish = features[features["imbalance"] > 0]["won_up"].mean()
bearish = features[features["imbalance"] < 0]["won_up"].mean()
print(f"{len(features)} markets, {len(df):,} book states")
print(f"Imbalance > 0 → Up won {bullish:.0%} | Imbalance < 0 → Up won {bearish:.0%}")
client.close()
