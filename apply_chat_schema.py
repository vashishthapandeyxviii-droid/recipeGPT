"""Creates the chat tables from schema_chat.sql. Safe to run more than once.
Run:  uv run python apply_chat_schema.py"""
from pathlib import Path

import db

SQL_FILE = Path(__file__).parent / "schema_chat.sql"
RECIPE_TABLES = ["recipes", "ingredients", "recipe_ingredients", "recipe_steps"]


def recipe_counts(conn):
    return {t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"] for t in RECIPE_TABLES}


def main():
    # drop comment lines, then run each statement (they are separated by semicolons)
    lines = [l for l in SQL_FILE.read_text(encoding="utf-8").splitlines() if not l.strip().startswith("--")]
    statements = [s.strip() for s in "\n".join(lines).split(";") if s.strip()]

    with db.get_connection() as conn:   # one transaction: all tables are created or none
        before = recipe_counts(conn)
        for statement in statements:
            conn.execute(statement)
        after = recipe_counts(conn)

    with db.get_connection() as conn:
        tables = [r["table_name"] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' ORDER BY table_name").fetchall()]
    print("Tables now in the database:", ", ".join(tables))
    print("Recipe tables unchanged:", before == after, after)


if __name__ == "__main__":
    main()
