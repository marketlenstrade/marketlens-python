"""Live order book depth and execution cost analysis across order sizes."""

from marketlens import MarketLens

client = MarketLens()

# Pick the first active market with a real two-sided book.
for m in client.markets.list(status="active", sort="-volume", take=20):
    book = client.orderbook.get(m.id)
    if book.bid_levels and book.ask_levels:
        break

print(f"market: {m.question}")
print(f"mid={book.midpoint}  spread={book.spread} ({book.spread_bps():.0f}bps)")
print(f"microprice={book.microprice()}  weighted_mid={book.weighted_midpoint(n=3)}")
print(f"imbalance: full={book.imbalance():.3f}  top3={book.imbalance(levels=3):.3f}")

bid_near, ask_near = book.depth_within(0.02)
print(f"depth within 2c: {bid_near} bid / {ask_near} ask")

for size in [100, 1_000, 5_000, 25_000]:
    avg = book.impact("BUY", size)
    if avg is None:
        print(f"  ${size:>6} → insufficient liquidity")
    else:
        bps = (avg - book.midpoint) / book.midpoint * 10_000
        print(f"  ${size:>6} → avg_fill={avg:.4f}  slippage={bps:.1f}bps")

client.close()
