from dotenv import dotenv_values
from nado_protocol.client import create_nado_client, NadoClientMode
from nado_protocol.indexer_client.types.query import IndexerProductSnapshotsParams

env = dotenv_values(".env")
pk = (env.get("NADO_PRIVATE_KEY") or "").strip()
if pk.startswith("0x"):
    pk = pk[2:]

client = create_nado_client(NadoClientMode.MAINNET, pk)

symbols = client.market.get_all_product_symbols()
items = symbols if isinstance(symbols, list) else getattr(symbols, "symbols", [])

rows = []
for s in items:
    pid = getattr(s, "product_id", None)
    sym = getattr(s, "symbol", None)
    try:
        snap = client.market.get_product_snapshots(IndexerProductSnapshotsParams(product_id=pid, limit=1))
        products = getattr(snap, "products", []) or []
        if not products:
            continue
        perp = products[0].product.perp
        min_notional = int(perp.book_info.min_size) / 1e18
        rows.append((pid, sym, min_notional))
    except Exception:
        pass

rows.sort(key=lambda x: (x[2], x[0]))

print("== min_notional 20 ==")
for pid, sym, mn in rows:
    if abs(mn - 20.0) < 1e-9:
        print(pid, sym, mn)

print("\n<= min_notional 20 ==")
for pid, sym, mn in rows:
    if mn <= 20.0:
        print(pid, sym, mn)