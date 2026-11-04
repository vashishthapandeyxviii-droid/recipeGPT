"""Saving and loading conversations in PostgreSQL.
Recipe data itself is still read through db.py; this file only handles chats.
Every function opens its own short connection, the same style as db.py."""
import db

DEFAULT_TITLE = "New chat"
MAX_TITLE = 40


class ConversationNotFound(Exception):
    pass


def make_title(text, max_len=MAX_TITLE):
    """Chat title from the first message: 'I have potatoes. What can I make?' stays as is,
    a long message is cut at a word boundary and ends with '...'."""
    t = " ".join((text or "").split())
    if not t:
        return DEFAULT_TITLE
    if len(t) <= max_len:
        return t
    cut = t[:max_len]
    space = cut.rfind(" ")
    if space >= max_len // 2:
        cut = cut[:space]
    return cut.rstrip(" ,.;:!?-") + "..."


def _iso(value):
    return value.isoformat() if value is not None else None


def _conversation(row):
    return {"id": row["id"], "title": row["title"],
            "created_at": _iso(row["created_at"]), "updated_at": _iso(row["updated_at"])}


def _recipes_by_id(cur, ids):
    """{id: full recipe dict}, built with the same function the recipe API uses."""
    ids = sorted({i for i in ids if i is not None})
    if not ids:
        return {}
    return {r["id"]: r for r in db._fetch_recipes(cur, ids)}


# ------------------------------------------------------------------ conversations
def create_conversation(title=None):
    with db.get_connection() as conn:
        row = conn.execute(
            "INSERT INTO conversations (title) VALUES (COALESCE(%s, 'New chat')) "
            "RETURNING id, title, created_at, updated_at", (title,)).fetchone()
    return _conversation(row)


def list_conversations(limit=100):
    """Most recently active first (what the sidebar shows)."""
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT id, title, created_at, updated_at FROM conversations "
            "ORDER BY updated_at DESC, id DESC LIMIT %s", (limit,)).fetchall()
    return [_conversation(r) for r in rows]


def get_conversation(conversation_id):
    """The conversation with all its messages. Each message carries the full recipes it suggested
    (in order) and the recipe it explained, so the page can draw them without more requests.
    Returns None if the conversation does not exist."""
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, title, created_at, updated_at FROM conversations WHERE id = %s", (conversation_id,))
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute("SELECT id, role, content, ai_used, focus_recipe_id, show_recipe, created_at "
                    "FROM messages WHERE conversation_id = %s ORDER BY id", (conversation_id,))
        messages = cur.fetchall()
        cur.execute("SELECT mr.message_id, mr.recipe_id FROM message_recipes mr "
                    "JOIN messages m ON m.id = mr.message_id "
                    "WHERE m.conversation_id = %s ORDER BY mr.message_id, mr.position", (conversation_id,))
        links = cur.fetchall()
        recipes = _recipes_by_id(cur, [l["recipe_id"] for l in links] + [m["focus_recipe_id"] for m in messages])

    suggested = {}
    for link in links:
        suggested.setdefault(link["message_id"], []).append(recipes[link["recipe_id"]])

    result = _conversation(row)
    result["messages"] = [{
        "id": m["id"], "role": m["role"], "content": m["content"], "ai_used": m["ai_used"],
        "show_recipe": m["show_recipe"], "created_at": _iso(m["created_at"]),
        "suggested_recipes": suggested.get(m["id"], []),
        "focus_recipe": recipes.get(m["focus_recipe_id"]),
    } for m in messages]
    return result


def conversation_summary(conversation_id):
    """id, title and times only (no messages). None if it does not exist."""
    with db.get_connection() as conn:
        row = conn.execute("SELECT id, title, created_at, updated_at FROM conversations WHERE id = %s",
                           (conversation_id,)).fetchone()
    return _conversation(row) if row else None


def rename_conversation(conversation_id, title):
    title = " ".join((title or "").split())
    if not title:
        raise ValueError("title must not be empty")
    with db.get_connection() as conn:
        row = conn.execute("UPDATE conversations SET title = %s WHERE id = %s RETURNING id",
                           (title[:80], conversation_id)).fetchone()
    return row is not None


def delete_conversation(conversation_id):
    """Deletes the conversation and (automatically) all its messages. Recipes are never touched."""
    with db.get_connection() as conn:
        row = conn.execute("DELETE FROM conversations WHERE id = %s RETURNING id", (conversation_id,)).fetchone()
    return row is not None


# ------------------------------------------------------------------ messages
def add_message(conversation_id, role, content, ai_used=False, suggested_ids=(), focus_recipe_id=None,
                show_recipe=False):
    """Save one message. Everything (message, its suggestions, the conversation's 'last active' time
    and the automatic title) is saved together or not at all.
    show_recipe=True means the page should draw the focus recipe as a full card."""
    if role not in ("user", "assistant"):
        raise ValueError("role must be 'user' or 'assistant'")
    content = (content or "").strip()
    if not content:
        raise ValueError("content must not be empty")
    ids = list(dict.fromkeys(suggested_ids))   # no duplicates, order kept

    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 AS ok FROM conversations WHERE id = %s", (conversation_id,))
        if cur.fetchone() is None:
            raise ConversationNotFound(conversation_id)
        cur.execute("INSERT INTO messages (conversation_id, role, content, ai_used, focus_recipe_id, show_recipe) "
                    "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id, created_at",
                    (conversation_id, role, content, ai_used, focus_recipe_id, show_recipe))
        message = cur.fetchone()
        for position, recipe_id in enumerate(ids, start=1):
            cur.execute("INSERT INTO message_recipes (message_id, recipe_id, position) VALUES (%s, %s, %s)",
                        (message["id"], recipe_id, position))
        cur.execute("UPDATE conversations SET updated_at = now() WHERE id = %s", (conversation_id,))
        if role == "user":   # the first user message names the chat
            cur.execute("UPDATE conversations SET title = %s WHERE id = %s AND title = %s",
                        (make_title(content), conversation_id, DEFAULT_TITLE))
        recipes = _recipes_by_id(cur, ids + [focus_recipe_id])

    return {"id": message["id"], "role": role, "content": content, "ai_used": ai_used,
            "show_recipe": show_recipe, "created_at": _iso(message["created_at"]),
            "suggested_recipes": [recipes[i] for i in ids],
            "focus_recipe": recipes.get(focus_recipe_id)}


def recent_messages(conversation_id, limit=6):
    """The last few messages, oldest first: [{'role':..., 'content':...}]. This is what Qwen will see."""
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT role, content FROM (SELECT id, role, content FROM messages "
            "WHERE conversation_id = %s ORDER BY id DESC LIMIT %s) latest ORDER BY id",
            (conversation_id, limit)).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in rows]


def get_context(conversation_id):
    """What 'the second one' and 'it' refer to right now:
       suggested = recipes of the latest message that suggested some, in order
       focus     = the recipe last explained in detail, but ONLY if that happened after the latest suggestions
                   (a newer suggestion list replaces it)."""
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT m.id FROM messages m WHERE m.conversation_id = %s AND m.role = 'assistant' "
                    "AND EXISTS (SELECT 1 FROM message_recipes mr WHERE mr.message_id = m.id) "
                    "ORDER BY m.id DESC LIMIT 1", (conversation_id,))
        last_suggest = cur.fetchone()
        cur.execute("SELECT id, focus_recipe_id FROM messages WHERE conversation_id = %s "
                    "AND focus_recipe_id IS NOT NULL ORDER BY id DESC LIMIT 1", (conversation_id,))
        last_focus = cur.fetchone()

        suggested_ids = []
        if last_suggest:
            cur.execute("SELECT recipe_id FROM message_recipes WHERE message_id = %s ORDER BY position", (last_suggest["id"],))
            suggested_ids = [r["recipe_id"] for r in cur.fetchall()]

        focus_id = None
        if last_focus and (last_suggest is None or last_focus["id"] > last_suggest["id"]):
            focus_id = last_focus["focus_recipe_id"]

        recipes = _recipes_by_id(cur, suggested_ids + [focus_id])

    return {"suggested": [recipes[i] for i in suggested_ids], "focus": recipes.get(focus_id)}
