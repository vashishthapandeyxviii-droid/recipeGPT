"""All PostgreSQL access for RecipeGPT lives in this file."""
import os

import psycopg
from psycopg.rows import dict_row

# The password is NOT stored in code. libpq (the engine under psycopg) reads it from the
# PGPASSWORD environment variable. To use a different server entirely, set DATABASE_URL.
DEFAULT_CONNINFO = "host=localhost port=5432 dbname=recipegpt user=postgres"


def get_connection():
    """Open a new connection. Rows come back as dictionaries (row['name'])."""
    conninfo = os.environ.get("DATABASE_URL", DEFAULT_CONNINFO)
    return psycopg.connect(conninfo, row_factory=dict_row)


def _number(q):
    """NUMERIC comes back as Decimal; turn it into 500 or 0.5 so the API output
    looks exactly like it did with the JSON file."""
    if q is None:
        return None
    q = float(q)
    return int(q) if q.is_integer() else q


def _fetch_recipes(cur, recipe_ids=None):
    """Build full recipe dictionaries (same shape as recipes.json) from the 4 tables.
    recipe_ids=None means all recipes."""
    if recipe_ids is None:
        cur.execute(
            "SELECT id, name, category, difficulty, cooking_time_minutes, servings "
            "FROM recipes ORDER BY id"
        )
    else:
        cur.execute(
            "SELECT id, name, category, difficulty, cooking_time_minutes, servings "
            "FROM recipes WHERE id = ANY(%s) ORDER BY id",
            (recipe_ids,),
        )
    recipes = {}
    for row in cur.fetchall():
        row["ingredients"] = []
        row["instructions"] = []
        recipes[row["id"]] = row
    if not recipes:
        return []

    ids = list(recipes)

    cur.execute(
        "SELECT ri.recipe_id, i.name, ri.quantity, ri.unit, ri.note "
        "FROM recipe_ingredients ri "
        "JOIN ingredients i ON i.id = ri.ingredient_id "
        "WHERE ri.recipe_id = ANY(%s) "
        "ORDER BY ri.recipe_id, ri.position",
        (ids,),
    )
    for row in cur.fetchall():
        ingredient = {"name": row["name"], "quantity": _number(row["quantity"]), "unit": row["unit"]}
        if row["note"]:
            ingredient["note"] = row["note"]
        recipes[row["recipe_id"]]["ingredients"].append(ingredient)

    cur.execute(
        "SELECT recipe_id, instruction FROM recipe_steps "
        "WHERE recipe_id = ANY(%s) ORDER BY recipe_id, step_number",
        (ids,),
    )
    for row in cur.fetchall():
        recipes[row["recipe_id"]]["instructions"].append(row["instruction"])

    return list(recipes.values())


def get_all_recipes():
    with get_connection() as conn:
        with conn.cursor() as cur:
            return _fetch_recipes(cur)


def get_known_ingredients():
    """Every distinct ingredient name, in the order they were first added."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM ingredients ORDER BY id")
            return [row["name"] for row in cur.fetchall()]


def find_recipes(ingredient_names):
    """Recipes containing ALL the given ingredient names (the Stage 1 rule, done in SQL)."""
    if not ingredient_names:
        return []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ri.recipe_id "
                "FROM recipe_ingredients ri "
                "JOIN ingredients i ON i.id = ri.ingredient_id "
                "WHERE i.name = ANY(%s) "
                "GROUP BY ri.recipe_id "
                "HAVING COUNT(DISTINCT i.name) = %s "
                "ORDER BY ri.recipe_id",
                (ingredient_names, len(set(ingredient_names))),
            )
            ids = [row["recipe_id"] for row in cur.fetchall()]
            return _fetch_recipes(cur, ids) if ids else []
