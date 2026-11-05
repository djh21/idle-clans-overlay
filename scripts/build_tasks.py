"""Fetch Idle Clans game data and write compact JSON for the overlay.

Output:
  data/items.json  {id: [name, baseValue, tradeable]}
  data/tasks.json  {"skills": {id: name}, "tasks": [ ... non-combat tasks ... ], "fetched": iso}
"""
import json, sys, urllib.request, datetime, os
os.makedirs("data", exist_ok=True)
def load_prev(name):
    try: return json.load(open("data/" + name))
    except Exception: return None
PREV = {n: load_prev(n) for n in ("items.json", "tasks.json", "combat.json", "gear.json")}

SKILLS = {1:"Rigour",2:"Strength",3:"Defence",4:"Archery",5:"Magic",6:"Health",7:"Crafting",8:"Woodcutting",9:"Carpentry",
          10:"Fishing",11:"Cooking",12:"Mining",13:"Smithing",14:"Foraging",15:"Farming",16:"Agility",17:"Plundering",
          18:"Enchanting",19:"Brewing",21:"Invocation"}
BASE = "https://query.idleclans.com/api"

import re
def clean(txt):
    # Mongo extended JSON tokens -> plain JSON
    txt = re.sub(r'ObjectId\("([0-9a-fA-F]+)"\)', r'"\1"', txt)
    txt = re.sub(r'ISODate\("([^"]+)"\)', r'"\1"', txt)
    txt = re.sub(r'Number(?:Long|Int|Decimal)\("?(-?[0-9.]+)"?\)', r'\1', txt)
    txt = re.sub(r'\bNaN\b', 'null', txt)
    return txt

def get(path):
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "idle-clans-overlay data updater"})
    with urllib.request.urlopen(req, timeout=120) as r:
        txt = r.read().decode("utf-8")
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        try:
            return json.loads(clean(txt))
        except json.JSONDecodeError as e:
            print("Could not parse", path, "->", repr(txt[max(0,e.pos-80):e.pos+80]), file=sys.stderr)
            raise

items = get("/Items/metadata")
items_out = {str(i["id"]): [i.get("name"), i.get("baseValue"), bool(i.get("tradeable")), i.get("associatedSkill"), i.get("equipmentSlot")] for i in items}

game = get("/Configuration/game-data?minVersion=0")
def find_groups(node, out):
    # any dict with an "Items" list whose entries carry a TaskId is a task group
    if isinstance(node, dict):
        items = node.get("Items")
        if isinstance(items, list) and items and isinstance(items[0], dict) and "TaskId" in items[0]:
            out.append(node); return
        for v in node.values(): find_groups(v, out)
    elif isinstance(node, list):
        for v in node: find_groups(v, out)
groups = []
find_groups(game, groups)
print("groups", len(groups), file=sys.stderr)

KEEP = ["TaskId","Name","Skill","LevelRequirement","BaseTime","ExpReward","ItemReward","ItemAmount","Costs",
        "Disabled","Hidden","AdditionalLevelRequirements","ItemIdRequired","BaseSuccessChancePercentage","TaskCategory"]
DEFAULTS = (None, 0, 0.0, False, "", [], {})
tasks = []
for g in groups:
    cid = g.get("CustomId", "")
    for t in g.get("Items", []):
        if (t.get("EnemyHealth") or 0) > 0 or t.get("IsBoss") or t.get("IsClanBoss"):
            continue
        row = {k: t.get(k) for k in KEEP if k in t}
        # keep any other non-default field so unknown mechanics (e.g. gold costs) are visible
        extra = {k: v for k, v in t.items() if k not in KEEP and v not in DEFAULTS and not k.startswith("Enemy")
                 and k not in ("SecondaryAttack","InvocationData","DescriptionLocKey","CustomIconId","UsedAttackStyle",
                               "AttackStyleWeakness","EnemyAttackInterval","EnemyRespawnTime","CanReceiveFromSkillingPartyEvent",
                               "AffectedByShowUsTheMoney","TriggerMilestoneOnCompletion","CanBeTeamed","EnableProtections",
                               "PreviousAttackStyleImmunity","IdentifiableType")}
        if extra: row["extra"] = extra
        row["group"] = cid
        tasks.append(row)

# --- clan upgrades (Type = numeric id used by /api/Clan/recruitment serializedUpgrades) and clan quest buffs
clan = {"fetched": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "upgrades": (game.get("ClanUpgrades") or {}).get("Items", []),
        "questRewards": (game.get("ClanQuestConfig") or {}).get("Rewards", []),
        "questConfig": {k: v for k, v in (game.get("ClanQuestConfig") or {}).items() if k in ("MaxActiveBuffs",)}}
json.dump(clan, open("data/clan.json", "w"), separators=(",", ":"))
if os.path.exists("data/probe.json"): os.remove("data/probe.json")

# --- combat: every enemy/boss task with its full definition (drop tables, stats), plus sibling config sections
combat = []
for g in groups:
    cid = g.get("CustomId", "")
    for t in g.get("Items", []):
        if (t.get("EnemyHealth") or 0) > 0 or t.get("IsBoss") or t.get("IsClanBoss"):
            row = {k: v for k, v in t.items() if v not in DEFAULTS and k not in ("DescriptionLocKey","CustomIconId","IdentifiableType")}
            row["group"] = cid
            combat.append(row)
cfg = {k: game.get(k) for k in ("SharedSettings","RaidsLevelInfos","ExterminatingInfo") if k in game}
json.dump({"fetched": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), "enemies": combat, "config": cfg},
          open("data/combat.json", "w"), separators=(",", ":"))

# --- equipment stats: every item with an equipment slot or a heal/boost value, trimmed of zero/false fields
allitems = (game.get("Items") or {}).get("Items", [])
SKIP = {"ExtraBoostPercentageAgainstQuestMonsters","Discontinued","ItemCounterpartId","CanNotBeSoldToGameShop","CanNotBeTraded","PlayerMarketBuyLimit",
        "IsGameShopToken","TradeableWithClan","IsClanBound","FlipSprite","Unobtainable","MasteryCapeType","Category","DescriptionLocKey","NameLocKey","CustomIconId","IdentifiableType"}
gear = []
for it in allitems:
    row = {k: v for k, v in it.items() if k not in SKIP and v not in DEFAULTS}; row["ItemId"] = it.get("ItemId", 0)
    if it.get("EquipmentSlot") or any(k for k in row if re.search(r'Heal|Boost|Interval|Bonus|Style|Requirement', k)):
        gear.append(row)
json.dump({"fetched": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), "keys": sorted({k for g in gear for k in g}), "items": gear},
          open("data/gear.json", "w"), separators=(",", ":"))
if os.path.exists("data/probe.json"): os.remove("data/probe.json")

out = {"skills": SKILLS, "fetched": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), "tasks": tasks}
import os; os.makedirs("data", exist_ok=True)
json.dump(items_out, open("data/items.json", "w"), separators=(",", ":"))
json.dump(out, open("data/tasks.json", "w"), separators=(",", ":"))
print("items", len(items_out), "tasks", len(tasks))

# --- update radar: what changed since the last run (kept for 60 days in data/changes.json)
def diff_run():
    changes = []
    def add(kind, what, detail=""): changes.append({"kind": kind, "what": what, "detail": detail})
    pi = PREV["items.json"] or {}
    for k, v in items_out.items():
        if k not in pi: add("item", v[0] or k, "new item" + (f", NPC value {v[1]}" if v[1] else ""))
        elif pi[k][1] != v[1]: add("item", v[0] or k, f"NPC value {pi[k][1]} → {v[1]}")
        elif bool(pi[k][2]) != bool(v[2]): add("item", v[0] or k, "now tradeable" if v[2] else "no longer tradeable")
    pt = {t.get("TaskId"): t for t in ((PREV["tasks.json"] or {}).get("tasks") or [])}
    names = {int(k): v[0] for k, v in items_out.items()}
    for t in tasks:
        o = pt.get(t.get("TaskId")); nm = str(t.get("Name", "")).replace("_", " ")
        if o is None: add("task", nm, f"new {SKILLS.get(t.get('Skill'), '')} task, level {t.get('LevelRequirement')}"); continue
        for f, label in (("BaseTime", "time"), ("ExpReward", "XP"), ("ItemAmount", "output qty"), ("LevelRequirement", "level"), ("Costs", "inputs"), ("Disabled", "disabled"), ("Hidden", "hidden")):
            if o.get(f) != t.get(f):
                a, b = o.get(f), t.get(f)
                if f == "Costs":
                    fmtc = lambda c: ", ".join(f"{x.get('Amount')} {names.get(x.get('Item'), x.get('Item'))}" for x in (c or []))
                    a, b = fmtc(a), fmtc(b)
                elif f == "BaseTime": a, b = f"{(a or 0)/1000:g}s", f"{(b or 0)/1000:g}s"
                add("task", nm, f"{label} {a} → {b}")
        if (o.get("extra") or {}).get("PlunderingLoot") != (t.get("extra") or {}).get("PlunderingLoot"): add("task", nm, "plundering loot table changed")
    pc = {e.get("TaskId"): e for e in ((PREV["combat.json"] or {}).get("enemies") or [])}
    for e in combat:
        o = pc.get(e.get("TaskId")); nm = str(e.get("Name", "")).replace("_", " ")
        if o is None: add("combat", nm, "new enemy"); continue
        for f in ("EnemyHealth", "EnemyDefenceLevel", "EnemyDefenceBonus", "EnemyAttackInterval", "AttackStyleWeakness", "ItemIdRequired", "LevelRequirement"):
            if o.get(f) != e.get(f): add("combat", nm, f"{f.replace('Enemy', '')} {o.get(f)} → {e.get(f)}")
        if o.get("Loot") != e.get("Loot"): add("combat", nm, "drop table changed")
    pg = {g.get("ItemId"): g for g in ((PREV["gear.json"] or {}).get("items") or [])}
    STATS = ("AccuracyBonus", "StrengthBonus", "DefenceBonus", "ArcheryAccuracyBonus", "ArcheryStrengthBonus", "ArcheryDefenceBonus", "MagicAccuracyBonus", "MagicStrengthBonus", "MagicDefenceBonus", "AttackInterval", "HealthAppliedOnConsume", "LevelRequirement")
    for g in gear:
        o = pg.get(g.get("ItemId")); nm = str(g.get("Name", "")).replace("_", " ")
        if o is None: continue  # new items already listed above
        ch = [f"{f} {o.get(f)} → {g.get(f)}" for f in STATS if o.get(f) != g.get(f)]
        if ch: add("gear", nm, "; ".join(ch))
    return changes
try:
    prev_changes = load_prev("changes.json") or {"runs": []}
    now = datetime.datetime.now(datetime.timezone.utc)
    if any(PREV.values()):
        ch = diff_run()
        if ch: prev_changes["runs"].append({"at": now.isoformat(timespec="seconds"), "changes": ch[:400]})
    cutoff = (now - datetime.timedelta(days=60)).isoformat()
    prev_changes["runs"] = [r for r in prev_changes["runs"] if r["at"] >= cutoff][-60:]
    prev_changes["checked"] = now.isoformat(timespec="seconds")
    json.dump(prev_changes, open("data/changes.json", "w"), separators=(",", ":"))
    print("changes this run", sum(len(r["changes"]) for r in prev_changes["runs"][-1:]) if prev_changes["runs"] else 0)
except Exception as ex:
    print("radar failed:", ex, file=sys.stderr)
