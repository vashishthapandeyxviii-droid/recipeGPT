import re

import httpx
import psycopg
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

import db  # our PostgreSQL helper file (db.py)

app = FastAPI()

# Ollama runs locally; FastAPI talks to it over plain HTTP
OLLAMA_URL = "http://localhost:11434/api/chat"
OLLAMA_MODEL = "qwen3:1.7b"
OLLAMA_TIMEOUT = 240  # seconds; the first request is slow while the model loads

SYSTEM_PROMPT = (
    "You are RecipeGPT, a friendly cooking assistant. "
    "Write 2 to 4 short, natural sentences of plain text, like a helpful friend chatting. "
    "Plain text only: no Markdown, no bullet points, no numbered lists, no bold markers, no emojis. "
    "Use ONLY the recipes provided below. "
    "Never invent recipes, ingredients, quantities or steps, "
    "and never suggest alternative recipes or ingredient substitutions. "
    "Mention each recipe by name at most once. If several match, talk about the best two or three; "
    "the full recipe cards are shown to the user separately. "
    "Do not copy the ingredient list, the steps or the recipe format. "
    "Just introduce the options briefly, and if you describe how a dish is made, "
    "use only that recipe's own steps and never mix up different recipes. "
    "Do not mention the category, difficulty, cooking time or servings "
    "unless the question asks about them (for example quick, easy, or for 4 people). "
    "Never write word counts or comments about your own answer.\n\n"
    "Example of a good reply.\n"
    "Recipe given: Lemon Rice (ingredients: rice, lemon, oil, salt; "
    "steps: cook the rice, then stir in lemon juice, oil and salt).\n"
    "Question: I have rice and lemon.\n"
    "Reply: You can make Lemon Rice with your rice and lemon! "
    "It is a simple dish where lemon juice is stirred into the cooked rice. "
    "The full recipe is in the card below."
)

# Common filler words. They are not ingredients, so we don't report them as "unknown".
STOPWORDS = {
    "i", "have", "has", "got", "with", "and", "or", "a", "an", "the", "some",
    "what", "can", "could", "should", "make", "cook", "using", "use", "from",
    "me", "my", "we", "you", "do", "does", "to", "of", "for", "in", "on",
    "recipe", "recipes", "dish", "dishes", "food", "something", "anything",
    "only", "just", "please", "want", "need", "how", "is", "are", "am", "any",
    "ingredient", "ingredients", "eat", "dinner", "lunch", "breakfast", "it",
}


def from_database(function, *args):
    """Run a db.py function and turn database problems into clear HTTP errors."""
    try:
        return function(*args)
    except psycopg.OperationalError:
        # server off, wrong password, unknown database, ...
        raise HTTPException(
            status_code=503,
            detail="Cannot connect to PostgreSQL. Is it running, and is PGPASSWORD set in this terminal?",
        )
    except psycopg.Error:
        # connected, but the query failed (e.g. tables missing)
        raise HTTPException(
            status_code=500,
            detail="Database error. Have you created the tables (schema.sql) and run migrate_json_to_postgres.py?",
        )


def load_recipes():
    return from_database(db.get_all_recipes)


@app.get("/recipes")
def get_recipes():
    return load_recipes()


class ChatRequest(BaseModel):
    ques: str = Field(max_length=500)  # longer input is rejected with a 422 error


def get_known_ingredients():
    """All distinct ingredient names (now read from the ingredients table)."""
    return from_database(db.get_known_ingredients)


def match_word(word, known):
    """Turn a word into a known ingredient, handling simple plurals.
    'potatoes' -> 'potato', 'onions' -> 'onion', 'chilies' -> 'chili'.
    Returns None if the word is not a known ingredient."""
    candidates = [word]
    if word.endswith("ies") and len(word) > 3:
        candidates.append(word[:-3] + "y")
    if word.endswith("es") and len(word) > 2:
        candidates.append(word[:-2])
    if word.endswith("s") and len(word) > 1:
        candidates.append(word[:-1])

    for c in candidates:
        if c in known:
            return c
    return None


def analyze_question(ques, known):
    """Split a sentence into (ingredients found, unrecognized words)."""
    # Keep letters only, so "potato, tomato!" -> ["potato", "tomato"]
    words = re.findall(r"[a-z]+", ques.lower())

    found = []
    unrecognized = []
    for w in words:
        ingredient = match_word(w, known)
        if ingredient:
            if ingredient not in found:  # duplicate prevention
                found.append(ingredient)
        elif w not in STOPWORDS and w not in unrecognized:
            unrecognized.append(w)
    return found, unrecognized


def find_recipes(found):
    """Recipes containing ALL requested ingredients (same rule as before, now done in SQL)."""
    return from_database(db.find_recipes, found)


def format_ingredient(ing):
    """{'name': 'potato', 'quantity': 500, 'unit': 'g'} -> '500 g potato'"""
    if ing["quantity"] is None:
        text = f"{ing['name']} ({ing['unit']})"      # e.g. "salt (to taste)"
    else:
        parts = [f"{ing['quantity']:g}"]             # 0.5 -> "0.5", 2.0 -> "2"
        if ing.get("unit"):
            parts.append(ing["unit"])
        parts.append(ing["name"])
        text = " ".join(parts)
    if ing.get("note"):
        text += f" ({ing['note']})"
    return text


def build_user_message(ques, matches):
    """The text sent to Qwen: the question plus the matched recipes."""
    blocks = []
    for r in matches[:5]:  # keep the prompt small for a tiny model
        steps = "\n".join(f"    {n}. {s}" for n, s in enumerate(r["instructions"], start=1))
        blocks.append(
            f"- {r['name']} ({r['category']}, {r['difficulty']}, "
            f"{r['cooking_time_minutes']} min, serves {r['servings']})\n"
            f"  Ingredients: {', '.join(format_ingredient(i) for i in r['ingredients'])}\n"
            f"  Steps:\n{steps}"
        )
    recipe_text = "\n".join(blocks)
    return f"Recipes I found:\n{recipe_text}\n\nUser question: {ques}"


def ask_ollama(ques, matches):
    """Send the question + matched recipes to Qwen via Ollama.
    Returns the answer text, or None if anything goes wrong."""
    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,   # wait for the full answer in one response
        "think": False,    # Qwen3: skip the visible reasoning
        "options": {"temperature": 0.3},  # lower = less creative, fewer inventions
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_message(ques, matches)},
        ],
    }
    try:
        response = httpx.post(OLLAMA_URL, json=payload, timeout=OLLAMA_TIMEOUT)
        response.raise_for_status()
        answer = response.json()["message"]["content"]
    except (httpx.HTTPError, KeyError, ValueError):
        # Ollama is off, too slow, or returned something unexpected
        return None

    # Safety net: remove any <think>...</think> block if one slips through
    answer = re.sub(r"<think>.*?</think>", "", answer, flags=re.DOTALL).strip()
    return answer or None


@app.post("/chat")
def chat(request: ChatRequest):
    known = get_known_ingredients()
    found, unrecognized = analyze_question(request.ques, known)

    base = {"ingredients_found": found, "unrecognized_words": unrecognized}

    if not found:
        if unrecognized:
            message = (
                "I don't know these ingredients yet: "
                + ", ".join(unrecognized)
                + ". Try ingredients like: "
                + ", ".join(known)
                + "."
            )
        else:
            message = "Please tell me which ingredients you have, e.g. 'I have potato and tomato'."
        return { 
            "status": "no_ingredients", "message": message,
            "answer": None, "ai_used": False, "matches": [], **base,
        }

    matches = find_recipes(found)
    note = ""
    if unrecognized:
        note = " (I ignored words I don't know as ingredients: " + ", ".join(unrecognized) + ")"

    if matches:
        # Only ask the AI when there are real recipes to talk about
        answer = ask_ollama(request.ques, matches)
        return {
            "status": "found",
            "message": f"You can make {len(matches)} recipe(s) with {', '.join(found)}.{note}",
            "answer": answer,
            "ai_used": answer is not None,
            "matches": matches,
            **base,
        }

    return {
        "status": "not_found",
        "message": f"Sorry, no recipe uses all of: {', '.join(found)}. Try fewer ingredients.{note}",
        "answer": None, "ai_used": False, "matches": [], **base,
    }


# Conversation endpoints (Stage 5) live in chat_api.py.
# Imported here, at the end, on purpose: chat_api itself uses functions from this file.
from chat_api import router as conversations_router  # noqa: E402

app.include_router(conversations_router)
