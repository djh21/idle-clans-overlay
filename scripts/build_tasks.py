"""Fetch Idle Clans game data and write compact JSON for the overlay.

Output:
  data/items.json  {id: [name, baseValue, tradeable]}
  data/tasks.json  {"skills": {id: name}, "tasks": [ ... non-combat tasks ... ], "fetched": iso}
"""
import json, sys, urllib.request, datetime

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

# --- probe: where do clan upgrades live in the feed? (small structural dump, no task payloads)
def key_paths(node, path="", depth=0, acc=None):
    acc = {} if acc is None else acc
    if depth > 3: return acc
    if isinstance(node, dict):
        for k, v in node.items():
            p = path + "/" + str(k)
            acc[p] = type(v).__name__ + (f"[{len(v)}]" if isinstance(v, (list, dict)) else "")
            if k != "Tasks": key_paths(v, p, depth + 1, acc)
    elif isinstance(node, list) and node:
        key_paths(node[0], path + "/[0]", depth + 1, acc)
    return acc
def find_upgrades(node, path="", hits=None):
    hits = [] if hits is None else hits
    if len(hits) > 60: return hits
    if isinstance(node, dict):
        blob = json.dumps(node)[:400]
        if re.search(r'clan_?upgrade|ClanUpgrade', blob, re.I) and not any(h["path"] in path for h in hits):
            hits.append({"path": path, "sample": blob}); return hits
        for k, v in node.items():
            if k != "Tasks": find_upgrades(v, path + "/" + str(k), hits)
    elif isinstance(node, list):
        for i, v in enumerate(node[:50]): find_upgrades(v, path + f"/[{i}]", hits)
    return hits
probe = {"paths": key_paths(game), "upgrade_hits": find_upgrades(game)}
import os; os.makedirs("data", exist_ok=True)
json.dump(probe, open("data/probe.json", "w"), indent=1)

out = {"skills": SKILLS, "fetched": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), "tasks": tasks}
import os; os.makedirs("data", exist_ok=True)
json.dump(items_out, open("data/items.json", "w"), separators=(",", ":"))
json.dump(out, open("data/tasks.json", "w"), separators=(",", ":"))
print("items", len(items_out), "tasks", len(tasks))
