"""Conversation endpoints for RecipeGPT (Stage 5).

main.py includes this router. The existing /recipes and /chat endpoints are not touched.

How one chat turn works (see _handle_turn):
  1. save the user's message
  2. work out what the user means (assistant.interpret): suggest dishes, show a recipe, answer a
     follow-up, ask back, or just chat. Which recipe "the second one" / "it" means comes from the
     saved conversation, never from Qwen.
  3. build the reply. The facts always come from PostgreSQL. Qwen only phrases the friendly text, and its
     text is checked (no other recipe names, no Markdown, ...). If Qwen is down or fails the check,
     a plain fixed text is used instead, so the chat always answers.
  4. save the reply (with the recipes it suggested / explained) and return both messages.
"""
import re

import httpx
import psycopg
from fastapi import APIRouter, HTTPException, Path
from pydantic import BaseModel, Field

import assistant
import chat_db
import db

router = APIRouter()

MAX_ID = 2**63 - 1          # largest value the database's BIGINT ids can hold
HISTORY_LIMIT = 6           # how many recent messages Qwen gets to see
DB_DOWN = "Cannot connect to PostgreSQL. Is it running, and is PGPASSWORD set in this terminal?"
DB_ERROR = "Database error. Have you created the tables (schema.sql and schema_chat.sql) and run the migrations?"

BASE_SYSTEM = (
    "You are RecipeGPT, a friendly cooking assistant in a chat. "
    "Write plain text only: no Markdown, no bullet points, no numbered lists, no bold text, no emojis. "
    "Use only the information given below. "
    "Never invent recipes, ingredients, quantities, steps, times or substitutions. "
    "Never write word counts or comments about your own answer."
)
MODE_SUGGEST = (
    "Introduce the recommended dishes in 2 to 4 friendly sentences. "
    "Mention each dish by its exact name once, in the order given. "
    "Mention extra ingredients the user would need only where they are listed. "
    "Mention cooking time or difficulty only if the user asked for something quick or easy. "
    "Do not give steps or quantities. Do not mention any other dish. "
    "Finish by inviting the user to pick one, for example by saying 'the second one'."
)
MODE_DETAIL = (
    "Write one or two short, friendly sentences introducing this recipe. "
    "Do not list ingredients or steps: the full recipe is shown to the user right below your message."
)
MODE_OTHER = (
    "Answer the user's question about this recipe in 1 to 3 plain sentences, using ONLY the recipe information below. "
    "If the information needed is not there (for example substitutions, nutrition, storage or tips), "
    "say you don't have that information. Do not guess. Do not mention any other recipe."
)
MODE_CHAT = (
    "Reply in 1 to 3 friendly sentences. Do not name or suggest any dish or recipe. "
    "If the user greets or thanks you, answer briefly and ask which ingredients they have. "
    "If the user asks a general cooking question, answer briefly with general cooking knowledge. "
    "If they mention an ingredient that is not in the list below, say you don't have recipes using it yet."
)
MODE_TIP = (
    "Give one short general cooking tip (one or two sentences) about the change the user asks for. "
    "It is general cooking knowledge, not part of the recipe. Do not say what the recipe contains beyond the "
    "ingredient list below. Do not mention any recipe by name."
)

# Text that is NOT stored recipe data is always labelled, so it can never be mistaken for the database.
GENERAL_LABEL = "General cooking tip (not from the recipe data): "
GENERAL_ADVICE_LABEL = "General cooking advice (not from the recipe database): "
GENERAL_FOOTER = "(This answer may include general cooking knowledge that is not stored in the recipe.)"
NO_SWAP_ADVICE = "I can't tell you how swapping or leaving out ingredients would change the dish, so I'd rather not guess."
COOKING_QUESTION = re.compile(r"\b(?:how (?:do|can|should|to|long|much)|what(?:'s| is| are)|why|can i|should i|is it safe|difference between)\b", re.IGNORECASE)


def _main():
    # imported late on purpose: main.py includes this router, so importing main at the top would be circular
    import main
    return main


# ------------------------------------------------------------------ request bodies
class ConversationIn(BaseModel):
    title: str | None = Field(default=None, max_length=80)


class TitleIn(BaseModel):
    title: str = Field(max_length=80)


class MessageIn(BaseModel):
    content: str = Field(max_length=500)    # same limit as the old /chat endpoint


def _guarded(function, *args, **kwargs):
    """Run something and turn database problems into clear HTTP errors."""
    try:
        return function(*args, **kwargs)
    except chat_db.ConversationNotFound:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    except psycopg.OperationalError:
        raise HTTPException(status_code=503, detail=DB_DOWN)
    except psycopg.Error:
        raise HTTPException(status_code=500, detail=DB_ERROR)


# ------------------------------------------------------------------ endpoints
@router.get("/conversations")
def list_conversations():
    return _guarded(chat_db.list_conversations)


@router.post("/conversations", status_code=201)
def create_conversation(body: ConversationIn | None = None):
    title = body.title.strip() if body and body.title and body.title.strip() else None
    return _guarded(chat_db.create_conversation, title)


@router.get("/conversations/{conversation_id}")
def read_conversation(conversation_id: int = Path(ge=1, le=MAX_ID)):
    conversation = _guarded(chat_db.get_conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return conversation


@router.patch("/conversations/{conversation_id}")
def rename_conversation(body: TitleIn, conversation_id: int = Path(ge=1, le=MAX_ID)):
    if not body.title.strip():
        raise HTTPException(status_code=422, detail="Title is empty.")
    if not _guarded(chat_db.rename_conversation, conversation_id, body.title):
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return _guarded(chat_db.conversation_summary, conversation_id)


@router.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: int = Path(ge=1, le=MAX_ID)):
    if not _guarded(chat_db.delete_conversation, conversation_id):
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return {"deleted": True}


@router.post("/conversations/{conversation_id}/messages")
def send_message(body: MessageIn, conversation_id: int = Path(ge=1, le=MAX_ID)):
    text = body.content.strip()
    if not text:
        raise HTTPException(status_code=422, detail="Message is empty.")
    return _guarded(_handle_turn, conversation_id, text)


# ------------------------------------------------------------------ one chat turn
def _handle_turn(conversation_id, text):
    m = _main()
    user_message = chat_db.add_message(conversation_id, "user", text)   # also proves the conversation exists

    all_recipes = db.get_all_recipes()
    known = db.get_known_ingredients()
    found, unrecognized = m.analyze_question(text, known)               # the Stage 1 ingredient finder
    context = chat_db.get_context(conversation_id)
    interp = assistant.interpret(text, found, all_recipes, context["suggested"], context["focus"])
    history = chat_db.recent_messages(conversation_id, limit=HISTORY_LIMIT)

    reply = _build_reply(m, interp, text, found, unrecognized, known, all_recipes, history)
    assistant_message = chat_db.add_message(
        conversation_id, "assistant", reply["text"], ai_used=reply["ai_used"],
        suggested_ids=reply["suggested_ids"], focus_recipe_id=reply["focus_id"],
        show_recipe=reply["show_recipe"])
    return {"conversation": chat_db.conversation_summary(conversation_id),
            "intent": interp.intent,
            "user_message": user_message,
            "assistant_message": assistant_message}


def _reply(text, ai_used=False, suggested=(), focus=None, show_recipe=False):
    return {"text": text, "ai_used": ai_used, "suggested_ids": [r["id"] for r in suggested],
            "focus_id": focus["id"] if focus else None, "show_recipe": show_recipe}


def _join(items, last="or"):
    """['a'] -> 'a'; ['a','b','c'] -> 'a, b or c' (or 'a, b and c' with last="and")."""
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + f" {last} " + items[-1]


def _build_reply(m, interp, text, found, unrecognized, known, all_recipes, history):
    intent = interp.intent

    if intent == "suggest":
        matches = db.find_recipes(interp.ingredients)                   # the "ALL ingredients" rule, in SQL
        note = f" (I don't know these ingredients yet: {', '.join(unrecognized[:3])}.)" if unrecognized else ""
        if not matches:
            return _reply(f"I couldn't find a recipe that uses all of: {', '.join(found)}. Try fewer ingredients.{note}")
        picks = assistant.pick_suggestions(matches, found)
        lines = []
        for number, r in enumerate(picks, start=1):
            extra = assistant.missing_ingredients(r, found)
            lines.append(f"{number}. {r['name']} ({r['cooking_time_minutes']} min, {r['difficulty']})"
                         + (f"; they would also need: {', '.join(extra)}" if extra else ""))
        block = (f"The user said: {text}\nIngredients they have: {', '.join(found)}\n"
                 f"Recommend exactly these dishes, best first:\n" + "\n".join(lines))
        if len(matches) > len(picks):
            block += f"\n({len(matches)} recipes match in total; you only recommend these {len(picks)}.)"
        answer = _generate(m, MODE_SUGGEST, block, history, picks, all_recipes, need_names=min(2, len(picks)))
        return _reply(answer or _suggest_fallback(picks, found, len(matches), note),
                      ai_used=answer is not None, suggested=picks)

    if intent == "detail":
        r = interp.recipe
        block = (f"The user wants to cook: {r['name']} ({r['category']}, {r['difficulty']}, "
                 f"{r['cooking_time_minutes']} min, serves {r['servings']}).")
        answer = _generate(m, MODE_DETAIL, block, history, [r], all_recipes, need_names=1, intro_only=True)
        fallback = f"Here's how to make {r['name']}. It takes about {r['cooking_time_minutes']} minutes and serves {r['servings']}."
        return _reply(answer or fallback, ai_used=answer is not None, focus=r, show_recipe=True)

    if intent == "followup":
        r = interp.recipe
        fixed = _fixed_answer(m, r, interp.topic)
        if fixed:                                                       # facts straight from the database
            return _reply(fixed, focus=r)
        if interp.ingredients:                                          # "Does it have chili?", "Without onion?"
            facts = _ingredient_facts(r, interp.ingredients)            # what the recipe really contains
            if not assistant.MODIFY.search(text):
                return _reply(facts, focus=r)
            listing = ", ".join(m.format_ingredient(i) for i in r["ingredients"])
            block = f"Recipe: {r['name']}. Its ingredient list: {listing}.\nThe user asks: {text}"
            tip = _generate(m, MODE_TIP, block, history, [], all_recipes, need_names=0)
            if tip:                                                     # advice is kept apart from the facts
                return _reply(f"{facts} {GENERAL_LABEL}{tip}", ai_used=True, focus=r)
            return _reply(f"{facts} {NO_SWAP_ADVICE}", focus=r)
        block = m.build_user_message(text, [r])
        answer = _generate(m, MODE_OTHER, block, history, [r], all_recipes, need_names=0)
        if answer:
            return _reply(f"{answer} {GENERAL_FOOTER}", ai_used=True, focus=r)
        fallback = (f"I can only answer from the details I have for {r['name']}: it takes about "
                    f"{r['cooking_time_minutes']} minutes and serves {r['servings']}. "
                    f"Ask me for its ingredients, or how to make it.")
        return _reply(fallback, focus=r)

    if intent == "clarify":
        asked_back = interp.reason in ("multiple", "ambiguous", "out_of_range")
        return _reply(assistant.clarification_text(interp), suggested=interp.candidates if asked_back else ())

    if intent == "more":
        return _reply('I suggest dishes from the ingredients you tell me. Tell me what you have, '
                      'for example "I have potatoes and onions".')

    # plain chat: greetings, thanks, unknown ingredients, anything else
    if unrecognized and re.search(r"\b(?:i have|i've got|i got|i've|have|got|using|with)\b", text, re.IGNORECASE):
        return _reply(f"I don't have recipes with {_join(unrecognized[:3], 'and')} yet. Some ingredients I know: "
                      f"{', '.join(known[:12])}. What do you have?")
    block = "Ingredients I have recipes for: " + ", ".join(known)
    answer = _generate(m, MODE_CHAT, block, history, [], all_recipes, need_names=0)
    if answer and len(text.split()) >= 4 and COOKING_QUESTION.search(text):
        answer = GENERAL_ADVICE_LABEL + answer                          # a general cooking answer, not database content
    return _reply(answer or 'Tell me which ingredients you have, for example "I have potatoes and onions", '
                            "and I'll suggest some dishes.", ai_used=answer is not None)


def _suggest_fallback(picks, found, total, note):
    parts = [f"With {_join(found, 'and')} you could make {_join(r['name'] for r in picks)}."]
    for r in picks:
        extra = assistant.missing_ingredients(r, found)
        if extra:
            parts.append(f"{r['name']} also needs {_join(extra, 'and')}.")
    if total > len(picks):
        parts.append(f"I found {total} recipes in all.")
    parts.append('Tell me which one you would like, for example "How do I make the second one?"')
    return " ".join(parts) + note


def _fixed_answer(m, r, topic):
    """Questions whose answer is a plain database fact need no AI. Returns None for other questions."""
    if topic == "time":
        return f"{r['name']} takes about {r['cooking_time_minutes']} minutes."
    if topic == "servings":
        return f"{r['name']} serves {r['servings']}."
    if topic == "difficulty":
        phrase = {"easy": "an easy recipe", "medium": "a medium-difficulty recipe", "hard": "a harder recipe"}[r["difficulty"]]
        return f"{r['name']} is {phrase}."
    if topic == "ingredients":
        return f"For {r['name']} you'll need: " + ", ".join(m.format_ingredient(i) for i in r["ingredients"]) + "."
    return None


def _ingredient_facts(r, found):
    """'Does it have chili?', 'Can I use butter instead of oil?': say which of those ingredients the recipe
    really contains, from the stored data."""
    have = {i["name"] for i in r["ingredients"]}
    present = [n for n in found if n in have]
    absent = [n for n in found if n not in have]
    parts = []
    if present:
        parts.append(f"{r['name']} uses {_join(present, 'and')}.")
    if absent:
        parts.append(f"It doesn't use {_join(absent)}." if present else f"{r['name']} doesn't use {_join(absent)}.")
    return " ".join(parts)


# ------------------------------------------------------------------ talking to Qwen, with checks
def _generate(m, mode, block, history, allowed, all_recipes, need_names, intro_only=False):
    """Ask Qwen for the friendly text. Returns None (-> the caller uses its fixed text) if Qwen is
    unreachable or its answer fails the checks."""
    past = list(history)
    while past and past[0]["role"] == "assistant":     # the chat history must start with the user
        past.pop(0)
    messages = [{"role": "system", "content": f"{BASE_SYSTEM}\n\n{mode}\n\n{block}"}] + past
    raw = _ask_qwen(m, messages)
    if raw is None:
        return None
    text = _clean(raw)
    return text if text and _acceptable(text, allowed, all_recipes, need_names, intro_only) else None


def _ask_qwen(m, messages):
    payload = {"model": m.OLLAMA_MODEL, "stream": False, "think": False,
               "options": {"temperature": 0.4}, "messages": messages}
    try:
        response = httpx.post(m.OLLAMA_URL, json=payload, timeout=m.OLLAMA_TIMEOUT)
        response.raise_for_status()
        answer = response.json()["message"]["content"]
    except (httpx.HTTPError, KeyError, ValueError):
        return None
    return re.sub(r"<think>.*?</think>", "", answer, flags=re.DOTALL).strip() or None


def _clean(text):
    """Remove Markdown and emojis if the model used them anyway."""
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"^\s*(?:[-*\u2022]|\d+[.)])\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


STEP_WORDS = re.compile(r"\b(?:first|then|next|finally|add|boil|mix|stir|drain|heat|fry|chop|slice|peel|mash|season|simmer|whisk|toast|knead)\b")


def _acceptable(text, allowed, all_recipes, need_names, intro_only=False):
    """Reject answers that name a recipe we did not give it, skip the dishes it should recommend,
    contain a word count, or ramble."""
    low = text.lower()
    allowed_names = {r["name"] for r in allowed}
    if any(r["name"].lower() in low for r in all_recipes if r["name"] not in allowed_names):
        return False
    if sum(1 for name in allowed_names if name.lower() in low) < need_names:
        return False
    if re.search(r"\b\d+\s*words?\b", low) or len(text) > 800:
        return False
    if intro_only and (len(text) > 250 or len(re.findall(r"[.!?](?:\s|$)", text)) > 2 or STEP_WORDS.search(low)):
        return False      # an intro must not retell (and possibly garble) the steps; the card has the real ones
    return True