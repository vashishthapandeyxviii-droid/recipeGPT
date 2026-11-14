import json
from pathlib import Path

FILE = Path(__file__).parent / "data" / "recipes.json"
CATEGORIES = {"breakfast", "main", "side", "soup", "snack", "salad", "dessert"}
DIFFICULTIES = {"easy", "medium", "hard"}
UNITS = {"g", "ml", "tsp", "tbsp", "slice", "clove", "to taste", None}

recipes = json.loads(FILE.read_text(encoding="utf-8"))
errors = []

def err(r, msg):
    errors.append(f"[{r.get('id')}] {r.get('name')}: {msg}")

ids, names, ingredient_sets = set(), set(), {}
for r in recipes:
    for key in ["id", "name", "category", "difficulty", "cooking_time_minutes",
                "servings", "ingredients", "instructions"]:
        if key not in r:
            err(r, f"missing field '{key}'")
    if r["id"] in ids: err(r, "duplicate id")
    ids.add(r["id"])
    if r["name"].lower() in names: err(r, "duplicate name")
    names.add(r["name"].lower())
    if r["category"] not in CATEGORIES: err(r, f"bad category {r['category']}")
    if r["difficulty"] not in DIFFICULTIES: err(r, f"bad difficulty {r['difficulty']}")
    if not (isinstance(r["cooking_time_minutes"], int) and r["cooking_time_minutes"] > 0): err(r, "bad cooking time")
    if not (isinstance(r["servings"], int) and r["servings"] > 0): err(r, "bad servings")
    if not r["instructions"] or not all(isinstance(s, str) and s.strip() for s in r["instructions"]):
        err(r, "instructions must be a non-empty list of strings")

    text = " ".join(r["instructions"]).lower()
    seen = []
    for i in r["ingredients"]:
        n = i["name"]
        if n != n.lower() or not n.isalpha(): err(r, f"ingredient name must be one lowercase word: {n!r}")
        if n in seen: err(r, f"duplicate ingredient {n}")
        seen.append(n)
        if i["unit"] not in UNITS: err(r, f"bad unit {i['unit']!r} for {n}")
        if i["quantity"] is None and i["unit"] != "to taste": err(r, f"{n}: no quantity needs unit 'to taste'")
        if i["quantity"] is not None and not (isinstance(i["quantity"], (int, float)) and i["quantity"] > 0):
            err(r, f"bad quantity for {n}")
        if n not in text: err(r, f"ingredient '{n}' never appears in the instructions")

    key = frozenset(seen)
    if key in ingredient_sets:
        err(r, f"same ingredients as '{ingredient_sets[key]}'")
    ingredient_sets[key] = r["name"]

print(f"{len(recipes)} recipes checked")
if errors:
    print("PROBLEMS:"); [print(" -", e) for e in errors]
else:
    print("All checks passed.")
