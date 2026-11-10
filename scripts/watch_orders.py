#!/usr/bin/env python3
"""Order watcher: compares Dan's market listings (data/watchlist.json) with the live order book.

Every run (GitHub Actions, ~5 min):
  * sell order undercut  -> someone lists below your price          -> Discord
  * buy order outbid     -> someone bids above your price           -> Discord
  * probably filled      -> your price level shrank / vanished while
                            you were at the front of the queue      -> Discord + data/trades.json
The API never says who owns a listing, so fills are inferred. Listings outside the top-10
ladder can't be seen; those orders are reported as "not visible".

  python scripts/watch_orders.py                  # alert mode
  python scripts/watch_orders.py --dry-run        # print status, change nothing
  python scripts/watch_orders.py --book f.json    # use saved {itemId: comprehensive} payloads (tests)

watchlist.json: {"updated": iso, "orders": [{"account","item","itemId","side":"sell"|"buy","price","qty"}]}
State (.watch_state.json, kept by actions/cache): per order: last level volume, remaining qty,
last alert signature.
"""
import argparse, datetime as dt, json, os, sys, time, urllib.request

BASE = "https://query.idleclans.com/api/PlayerMarket/items/prices/latest/comprehensive/"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WATCH = os.path.join(ROOT, "data", "watchlist.json")
TRADES = os.path.join(ROOT, "data", "trades.json")
STATE = os.path.join(ROOT, ".watch_state.json")


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def dump(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=1)
        f.write("\n")


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "idle-clans-overlay order watcher"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def ladder(entries):
    """[{key: price, value: volume}] (or {price, volume} / [p, v]) -> {price: volume}"""
    out = {}
    for e in entries or []:
        if isinstance(e, dict):
            p = e.get("key", e.get("Key", e.get("price", e.get("Price"))))
            v = e.get("value", e.get("Value", e.get("volume", e.get("Volume"))))
        else:
            p, v = e[0], e[1]
        if p is not None and v is not None:
            out[int(p)] = out.get(int(p), 0) + int(v)
    return out


def oid(o):
    return f"{o['account']}|{o['itemId']}|{o['side']}|{o['price']}"


def fmt(n):
    return f"{int(n):,}"


def check(o, book, st):
    """Return (events, status_line). Mutates st (per-order state)."""
    side, P = o["side"], int(o["price"])
    asks, bids = ladder(book.get("lowestSellPricesWithVolume")), ladder(book.get("highestBuyPricesWithVolume"))
    mine = asks if side == "sell" else bids
    other = bids if side == "sell" else asks
    remaining = st.get("remaining", int(o["qty"]))
    level = mine.get(P, 0)
    if side == "sell":
        ahead = {p: v for p, v in asks.items() if p < P}
        visible = P in asks or (asks and (len(asks) < 10 or P <= max(asks)))
    else:
        ahead = {p: v for p, v in bids.items() if p > P}
        visible = P in bids or (bids and (len(bids) < 10 or P >= min(bids)))
    events = []
    name, acct = o["item"], o["account"]

    # --- fills: only trust a drop at your level when nobody was ahead of you last time
    prev = st.get("level")
    if prev is not None and remaining > 0 and not st.get("ahead_prev") and visible:
        drop = prev - level
        if drop > 0:
            sold = min(drop, remaining) if level > 0 else remaining
            remaining -= sold
            events.append(("fill", f"✅ **Probably {'sold' if side == 'sell' else 'bought'}** {fmt(sold)} {name} @ {fmt(P)} ({acct})"
                           + (f" — {fmt(remaining)} left" if remaining > 0 else " — order complete"), sold))
    st["level"], st["remaining"], st["ahead_prev"] = level, remaining, bool(ahead)

    # --- undercut / outbid
    if remaining > 0 and ahead:
        best = min(ahead) if side == "sell" else max(ahead)
        sig = f"{best}"
        if st.get("alerted") != sig:
            vol = sum(ahead.values())
            if side == "sell":
                events.append(("undercut", f"⚠️ **Undercut** — {name} ({acct}): {fmt(vol)} listed below your {fmt(P)}, cheapest {fmt(best)}"
                               + (f". Best bid is {fmt(max(bids))}." if bids else ".")))
            else:
                events.append(("outbid", f"📈 **Outbid** — {name} ({acct}): {fmt(vol)} wanted above your {fmt(P)}, top bid {fmt(best)}"
                               + (f". Cheapest ask is {fmt(min(asks))}." if asks else ".")))
            st["alerted"] = sig
    elif not ahead and st.get("alerted"):
        events.append(("front", f"🟢 Back at the front — {name} ({acct}) {side} @ {fmt(P)}"))
        st["alerted"] = None

    if remaining <= 0:
        status = "filled"
    elif not visible:
        status = "not visible (outside top 10)"
    elif ahead:
        status = f"{'undercut' if side == 'sell' else 'outbid'} ({'cheapest' if side == 'sell' else 'top'} {fmt(min(ahead) if side == 'sell' else max(ahead))})"
    else:
        status = "at the front"
    return events, status, remaining


def post(webhook, lines):
    msg = "\n".join(lines)
    while msg:
        chunk, msg = msg[:1900], msg[1900:]
        body = json.dumps({"content": chunk, "allowed_mentions": {"parse": []}}).encode()
        req = urllib.request.Request(webhook, data=body, headers={"Content-Type": "application/json", "User-Agent": "idle-clans-overlay order watcher"})
        urllib.request.urlopen(req, timeout=30).read()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--book", help="JSON file {itemId: comprehensive payload} instead of fetching")
    args = ap.parse_args()

    wl = load(WATCH, {"orders": []})
    orders = [o for o in wl.get("orders", []) if o.get("itemId") is not None and o.get("status") != "filled"]
    if not orders:
        print("watchlist empty")
        return 0
    books = load(args.book, {}) if args.book else {}
    for iid in sorted({int(o["itemId"]) for o in orders}):
        if str(iid) not in books:
            try:
                books[str(iid)] = get_json(BASE + str(iid))
                time.sleep(0.3)
            except Exception as e:  # keep going for other items
                print("fetch failed", iid, e)

    state = load(STATE, {})
    if wl.get("updated") != state.get("_watchlist"):          # new snapshot: forget old levels so a relist isn't read as a fill
        state = {"_watchlist": wl.get("updated")}
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    lines, new_trades, changed = [], [], False
    for o in orders:
        book = books.get(str(int(o["itemId"])))
        if not book:
            continue
        st = state.setdefault(oid(o), {})
        events, status, remaining = check(o, book, st)
        print(f"{o['account']:12} {o['side']:4} {o['item'][:28]:28} @ {o['price']:>8} x{o.get('qty')} -> {status}")
        for kind, text, *rest in events:
            lines.append(text)
            if kind == "fill":
                new_trades.append({"at": now, "account": o["account"], "item": o["item"], "itemId": o["itemId"], "side": o["side"],
                                   "price": o["price"], "qty": rest[0], "how": "inferred from order book"})
        if o.get("remaining") != remaining or o.get("watchStatus") != status:
            o["remaining"], o["watchStatus"] = remaining, status
            if remaining <= 0:
                o["status"] = "filled"
            changed = True

    if args.dry_run:
        print("\n".join(lines) or "(no events)")
        return 0
    dump(STATE, state)
    if new_trades:
        tr = load(TRADES, {"trades": []})
        tr["trades"].extend(new_trades)
        dump(TRADES, tr)
    if changed or new_trades:
        wl["checked"] = now
        dump(WATCH, wl)
    webhook = os.environ.get("DISCORD_WEBHOOK", "").strip()
    if lines:
        if webhook:
            post(webhook, lines)
            print("posted", len(lines), "line(s)")
        else:
            print("(no DISCORD_WEBHOOK)\n" + "\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
