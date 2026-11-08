#!/usr/bin/env python3
"""NPC-floor sniper.

Polls the live market and flags any item whose lowest sell (ask) price is below what the
NPC shop pays for it (base value x clan +10% x negotiation +5%, no tax on NPC sales).
Buying those listings and selling them to the NPC is risk-free profit.

  python scripts/npc_sniper.py                 # alert mode (posts to $DISCORD_WEBHOOK if set)
  python scripts/npc_sniper.py --dry-run       # print the 15 items nearest the floor, post nothing
  python scripts/npc_sniper.py --latest f.json # use a saved /latest payload (tests)

Env / flags:
  DISCORD_WEBHOOK   webhook URL (repo secret). Missing -> alerts are only printed.
  NPC_CLAN=1        clan upgrade "An offer they can't refuse" owned (x1.10). 0 to disable.
  NPC_NEGO=1        Last negotiation / Potion of negotiation (x1.05). 0 to disable.
  MIN_PROFIT=1      minimum profit per unit (gold) to alert.
  MIN_TOTAL=250     minimum profit x units available at that ask to alert.
  COOLDOWN_MIN=60   don't re-alert the same item at the same-or-higher ask within this window.
State (dedupe) lives in .sniper_state.json (persisted between runs by actions/cache).
"""
import argparse, json, math, os, sys, time, urllib.request

BASE = "https://query.idleclans.com/api"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATE_PATH = os.path.join(ROOT, ".sniper_state.json")


def env_num(name, default):
    v = os.environ.get(name, "")
    try:
        return float(v) if v != "" else default
    except ValueError:
        return default


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "idle-clans-overlay npc sniper"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def npc_floor(base, clan, nego):
    """Gold the NPC pays per unit. Floored: the game rounds down, so this never overstates."""
    return math.floor(base * (1.10 if clan else 1.0) * (1.05 if nego else 1.0))


def scan(latest, items, clan, nego):
    """Return every tradeable item with its ask, NPC floor and profit, sorted by profit/unit."""
    rows = []
    for it in latest:
        iid = str(it.get("itemId"))
        meta = items.get(iid)
        if not meta:
            continue
        name, base = meta[0], meta[1]
        ask = it.get("lowestSellPrice") or 0
        if not base or ask <= 0:
            continue
        floor = npc_floor(base, clan, nego)
        vol = it.get("lowestPriceVolume") or 0
        rows.append({
            "id": iid, "name": name, "ask": ask, "floor": floor, "base": base,
            "profit": floor - ask, "vol": vol, "total": (floor - ask) * vol,
            "bid": it.get("highestBuyPrice") or 0, "avg": it.get("dailyAveragePrice") or 0,
        })
    rows.sort(key=lambda r: (-r["profit"], r["ask"]))
    return rows


def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f)


def pick_alerts(rows, state, min_profit, min_total, cooldown_s, now):
    """Filter hits and drop ones already alerted recently at the same-or-better ask."""
    out = []
    for r in rows:
        if r["profit"] < min_profit or r["total"] < min_total:
            continue
        prev = state.get(r["id"])
        if prev and now - prev["t"] < cooldown_s and r["ask"] >= prev["ask"]:
            continue  # same opportunity, already pinged
        out.append(r)
    return out


def fmt_gold(n):
    n = int(n)
    if abs(n) >= 1_000_000:
        return f"{n/1_000_000:.2f}m"
    if abs(n) >= 10_000:
        return f"{n/1000:.1f}k"
    return f"{n:,}"


def build_message(alerts, clan, nego):
    mult = (1.10 if clan else 1.0) * (1.05 if nego else 1.0)
    lines = [f"**NPC-floor sniper** — {len(alerts)} item{'s' if len(alerts) != 1 else ''} listed below NPC price (floor = base × {mult:.3f})"]
    for r in alerts[:15]:
        lines.append(
            f"• **{r['name']}** — buy @ **{fmt_gold(r['ask'])}**, NPC pays **{fmt_gold(r['floor'])}** "
            f"→ +{fmt_gold(r['profit'])}/ea × {fmt_gold(r['vol'])} avail = **+{fmt_gold(r['total'])}** "
            f"(7d avg {fmt_gold(r['avg'])})"
        )
    if len(alerts) > 15:
        lines.append(f"…and {len(alerts) - 15} more.")
    lines.append("_Prices move fast — check the ladder before buying._")
    return "\n".join(lines)


def post_discord(webhook, content):
    body = json.dumps({"content": content[:1990], "allowed_mentions": {"parse": []}}).encode()
    req = urllib.request.Request(webhook, data=body, headers={
        "Content-Type": "application/json", "User-Agent": "idle-clans-overlay npc sniper"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print nearest-to-floor table, post nothing")
    ap.add_argument("--latest", help="path to a saved /latest JSON payload instead of fetching")
    ap.add_argument("--items", default=os.path.join(ROOT, "data", "items.json"))
    args = ap.parse_args()

    clan = env_num("NPC_CLAN", 1) != 0
    nego = env_num("NPC_NEGO", 1) != 0
    min_profit = env_num("MIN_PROFIT", 1)
    min_total = env_num("MIN_TOTAL", 250)
    cooldown_s = env_num("COOLDOWN_MIN", 60) * 60
    webhook = os.environ.get("DISCORD_WEBHOOK", "").strip()

    with open(args.items) as f:
        items = json.load(f)
    if args.latest:
        with open(args.latest) as f:
            latest = json.load(f)
    else:
        latest = get_json(BASE + "/PlayerMarket/items/prices/latest?includeAveragePrice=true")

    rows = scan(latest, items, clan, nego)
    now = time.time()

    if args.dry_run:
        print(f"{len(rows)} priced items; floor multiplier clan={clan} nego={nego}")
        print(f"{'item':34} {'ask':>8} {'floor':>7} {'profit':>7} {'vol':>7} {'total':>9}")
        for r in rows[:15]:
            print(f"{r['name'][:34]:34} {r['ask']:>8} {r['floor']:>7} {r['profit']:>7} {r['vol']:>7} {r['total']:>9}")
        return 0

    state = load_state()
    alerts = pick_alerts(rows, state, min_profit, min_total, cooldown_s, now)
    hits = [r for r in rows if r["profit"] >= min_profit]
    print(f"{len(rows)} priced items, {len(hits)} below floor, {len(alerts)} new alert(s)")
    for r in hits:
        print(f"  {r['name']}: ask {r['ask']} floor {r['floor']} +{r['profit']}/ea x{r['vol']}")

    if alerts:
        msg = build_message(alerts, clan, nego)
        if webhook:
            print("Discord:", post_discord(webhook, msg))
        else:
            print("(no DISCORD_WEBHOOK set)\n" + msg)
        for r in alerts:
            state[r["id"]] = {"ask": r["ask"], "t": now}

    # forget stale entries so the file doesn't grow forever
    state = {k: v for k, v in state.items() if now - v["t"] < 7 * 86400}
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
