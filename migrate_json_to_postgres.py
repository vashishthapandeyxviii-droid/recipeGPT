"""One-time copy of data/recipes.json into PostgreSQL.
Run:            uv run python migrate_json_to_postgres.py
Start over:     uv run python migrate_json_to_postgres.py --reset
recipes.json is only READ, never changed or deleted."""
import json
import sys
from pathlib import Path

import db

JSON_FILE = Path(__file__).parent / "data" / "recipes.json"


def main():
    recipes = json.loads(JSON_FILE.read_text(encoding="utf-8"))
    reset = "--reset" in sys.argv

    # "with" = one transaction: if anything fails, NOTHING is saved.
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM recipes")
            if cur.fetchone()["n"] > 0:
                if not reset:
                    print("The recipes table already has data. Nothing changed.")
                    print("To wipe the database tables and re-import, run with --reset.")
                    return
                cur.execute(
                    "TRUNCATE recipe_steps, recipe_ingredients, recipes, ingredients "
                    "RESTART IDENTITY CASCADE"
                )
                print("Existing data cleared (--reset).")

            # 1) Each distinct ingredient once
            ingredient_ids = {}
            for r in recipes:
                for ing in r["ingredients"]:
                    if ing["name"] not in ingredient_ids:
                        cur.execute(
                            "INSERT INTO ingredients (name) VALUES (%s) RETURNING id",
                            (ing["name"],),
                        )
                        ingredient_ids[ing["name"]] = cur.fetchone()["id"]

            # 2) Recipes, their ingredient links, and their steps
            for r in recipes:
                cur.execute(
                    "INSERT INTO recipes (id, name, category, difficulty, cooking_time_minutes, servings) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (r["id"], r["name"], r["category"], r["difficulty"],
                     r["cooking_time_minutes"], r["servings"]),
                )
                for position, ing in enumerate(r["ingredients"], start=1):
                    cur.execute(
                        "INSERT INTO recipe_ingredients "
                        "(recipe_id, ingredient_id, position, quantity, unit, note) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        (r["id"], ingredient_ids[ing["name"]], position,
                         ing["quantity"], ing["unit"], ing.get("note")),
                    )
                for step_number, text in enumerate(r["instructions"], start=1):
                    cur.execute(
                        "INSERT INTO recipe_steps (recipe_id, step_number, instruction) "
                        "VALUES (%s, %s, %s)",
                        (r["id"], step_number, text),
                    )

            # We inserted our own ids (1..24), so move the id counter past them.
            # Otherwise the next new recipe would try to use id 1 and fail.
            cur.execute(
                "SELECT setval(pg_get_serial_sequence('recipes', 'id'), (SELECT MAX(id) FROM recipes))"
            )
    # leaving the "with" block committed everything

    # Verify using a fresh connection: is the database identical to the JSON file?
    from_db = db.get_all_recipes()
    print(f"Recipes in JSON: {len(recipes)} | in PostgreSQL: {len(from_db)}")
    print(f"Distinct ingredients: {len(ingredient_ids)}")
    if from_db == recipes:
        print("SUCCESS: PostgreSQL data matches recipes.json exactly.")
    else:
        print("WARNING: data differs from recipes.json. Do not continue; send me this output.")
        for a, b in zip(recipes, from_db):
            if a != b:
                print("First difference at recipe:", a["name"])
                break


if __name__ == "__main__":
    main()
