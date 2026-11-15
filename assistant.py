"""RecipeGPT conversation logic.

Pure functions only: no database, no Ollama, no web code. That makes them easy to test.
The caller (the API, in a later stage) supplies:
  - the ingredients already found in the message (main.analyze_question does that)
  - all recipes, the recipes suggested in the latest suggestion message (in order),
    and the recipe currently being discussed ("focus"), if any.
"""
import re
from collections import Counter
from dataclasses import dataclass, field

TOP_N = 3                                  # how many dishes to suggest at once
PANTRY_STAPLES = {"salt", "oil", "water"}  # assumed to be in every kitchen, never "missing"


# ------------------------------------------------------------------ ranking
def missing_ingredients(recipe, requested):
    """Ingredients the user did not mention (pantry staples excluded)."""
    have = set(requested)
    return [i["name"] for i in recipe["ingredients"]
            if i["name"] not in have and i["name"] not in PANTRY_STAPLES]


def rank_recipes(matches, requested):
    """Best first: fewest missing ingredients, then shortest cooking time, then lowest id."""
    return sorted(matches, key=lambda r: (len(missing_ingredients(r, requested)),
                                          r["cooking_time_minutes"], r["id"]))


def pick_suggestions(matches, requested, exclude_ids=(), n=TOP_N):
    excluded = set(exclude_ids)
    return [r for r in rank_recipes(matches, requested) if r["id"] not in excluded][:n]


# ------------------------------------------------------------------ text helpers
def _singular(word):
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith("oes"):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def normalize(text):
    """lower-case, letters and digits only, plurals folded: 'Masala Potatoes!' -> 'masala potato'.
    Used on BOTH the user's text and the recipe names, so they compare fairly."""
    return " ".join(_singular(w) for w in re.findall(r"[a-z0-9]+", text.lower()))


def _phrase_in(text_norm, phrase_norm):
    return f" {phrase_norm} " in f" {text_norm} "


# ------------------------------------------------------------------ what is the user asking?
I = re.IGNORECASE
STRICT_DETAIL = re.compile(
    r"\bhow\s+(?:do|can|should|would|could|to)\b[^?.!]*\b(?:make|cook|prepare|bake|fry)\b"
    r"|\b(?:steps?|instructions?|method|directions?|procedure)\b"
    r"|\bwalk me through\b|\bfull recipe\b|\brecipe for\b|\bhow is (?:it|that|this) made\b", I)
SOFT_DETAIL = re.compile(
    r"\bshow me\b|\btell me (?:more|about)\b"
    r"|\bi(?:'ll| will) (?:make|cook|take|try|go with|choose|pick)\b"
    r"|\bi(?:'d| would) (?:like to |rather )?(?:make|cook|try)\b"
    r"|\blet'?s (?:make|cook|try|do)\b|\bgo with\b", I)
TOPICS = [
    ("time", re.compile(r"\bhow long\b|\bhow much time\b|\b(?:cooking|prep|preparation) time\b|\bminutes?\b|\bquick(?:ly)?\b|\bhours?\b", I)),
    ("servings", re.compile(r"\bserv(?:e|es|ed|ing|ings)\b|\bhow many (?:people|persons|portions|servings)\b|\bportions?\b|\bfeed\b", I)),
    ("ingredients", re.compile(r"\bingredients?\b|\bwhat (?:do|will|would|should|can) i need\b|\bneed to (?:buy|get)\b|\bshopping\b|\bwhat(?:'s| is) in\b|\bwhat goes in\b", I)),
    ("difficulty", re.compile(r"\b(?:hard|difficult|difficulty|easy|complicated|beginners?)\b", I)),
]
QUESTION = re.compile(r"\?|^\s*(?:can|could|is|are|does|do|what|why|which|will|would|should|how)\b", I)
# General cooking questions that merely MENTION an ingredient ("How do I chop an onion?") are not recipe searches
_TECHNIQUE = (r"chop|cut|slice|dice|mince|peel|store|keep|freeze|thaw|reheat|boil|fry|bake|roast|grate|mash|season|"
              r"marinate|clean|wash|sharpen|soften|brown|caramelize|caramelise|steam|microwave")
GENERAL_COOKING = re.compile(
    rf"\bhow\s+(?:do|can|should|would)\s+(?:i|you|we)\s+(?:{_TECHNIQUE})\b|\bhow\s+to\s+(?:{_TECHNIQUE})\b"
    r"|\bhow\s+long\s+(?:can|does|do|should|will)\b.*\b(?:keep|last|store|freeze|rest|stay)\b"
    r"|\bwhat(?:'s| is) the difference between\b|\bis it safe to\b|\bwhy\s+(?:do|does|is|are)\b"
    r"|\bcan\s+(?:i|you)\s+(?:freeze|reheat|store|microwave|refreeze)\b", I)
# ...unless the user is clearly telling us what they have or asking for dishes
SEARCH_HINT = re.compile(r"\b(?:i have|i've got|i got|got|have|with|using|leftovers?|what can i (?:make|cook|do)|recipes?|dishes?|ideas?)\b", I)
MORE = re.compile(
    r"\bwhat else\b|\banything else\b|\bsomething else\b|\bshow me more\b|\bany other\b"
    r"|\bmore (?:options|ideas|dishes|recipes|suggestions|choices)\b"
    r"|\bother (?:options|ideas|dishes|recipes|suggestions|choices)\b"
    r"|\banother (?:one|option|idea|dish|suggestion)\b", I)
PRONOUN = re.compile(r"\b(?:it|that|this)\b", I)
# "Does it have chili?", "Is there onion in it?", "Can I add chili to it?": about the recipe's contents
CONTENT_QUESTION = re.compile(
    r"\b(?:does|do|is|are)\b.*\b(?:it|that|this)\b.*\b(?:use|uses|have|has|contain|contains|include|includes|need|needs)\b"
    r"|\bis there\b.*\b(?:in|inside)\b.*\b(?:it|that|this)\b"
    r"|\b(?:in|inside)\s+(?:it|that|this)\b"
    r"|\badd\b.*\b(?:to|into)\s+(?:it|that|this)\b", I)
# "without chili", "instead of oil", "I don't have onions": a question ABOUT a recipe, not a new search
MODIFY = re.compile(r"\bwithout\b|\binstead of\b|\breplace\b|\bsubstitut\w*|\bswap\b|\bskip\b|\bomit\b|\bleave (?:out|off)\b"
                    r"|\b(?:don't|do not|dont) have\b|\bout of\b|\bextra\b", I)


def _topic_of(text):
    for name, pattern in TOPICS:
        if pattern.search(text):
            return name
    return None


def _has_recipe_intent(text):
    return bool(STRICT_DETAIL.search(text) or _topic_of(text) or SOFT_DETAIL.search(text) or QUESTION.search(text))


# Words that, on their own, only POINT at a recipe: "What about the second one?", "and the third?", "number 2 please"
BARE_WORDS = {"the", "one", "first", "second", "third", "fourth", "fifth", "last", "final", "1st", "2nd", "3rd", "4th",
              "5th", "and", "what", "how", "about", "please", "then", "number", "option", "recipe", "dish", "idea",
              "choice", "that", "this", "it", "show", "me", "ok", "okay", "go", "with", "i", "i'll", "ill", "will",
              "take", "choose", "pick", "a", "an", "of", "for", "can", "you", "tell", "more", "let's", "lets", "make",
              "cook", "try", "do", "next", "also", "now"}


def _is_bare_reference(text):
    words = re.findall(r"[a-z0-9']+", text.lower())
    return bool(words) and all(w in BARE_WORDS or w.isdigit() for w in words)


def _classify_recipe_request(text):
    """('detail', None) = show the full recipe; ('followup', topic) = answer a specific question."""
    if STRICT_DETAIL.search(text) or _is_bare_reference(text):
        return "detail", None
    topic = _topic_of(text)
    if topic:
        return "followup", topic
    if SOFT_DETAIL.search(text):
        return "detail", None
    if QUESTION.search(text):
        return "followup", "other"
    return "detail", None      # a bare reference like "the second one" means "show me that recipe"


# ------------------------------------------------------------------ which recipe does the user mean?
ORD = r"(first|1st|second|2nd|third|3rd|fourth|4th|fifth|5th|last|final)"
NOT_AFTER = r"(?!\s+(?:step|steps|time|thing|part|minute|minutes|ingredient|ingredients|stage|course|few|half|batch))"
ORDINAL_PATTERNS = [
    re.compile(rf"\bthe\s+{ORD}\b{NOT_AFTER}", I),                                  # "the second", "the last"
    re.compile(rf"\b{ORD}\s+(?:one|recipe|dish|option|choice|idea|suggestion)\b", I),  # "second one"
]
NUMBER_PATTERNS = [
    re.compile(r"\b(?:number|option|recipe|dish|choice|idea)\s*#?\s*(\d)\b", I),    # "number 2"
    re.compile(r"#\s*(\d)\b"),                                                      # "#2"
    re.compile(r"^\s*(\d)\s*[.!?]*\s*$"),                                           # just "2"
]
WORD_TO_POSITION = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3,
                    "fourth": 4, "4th": 4, "fifth": 5, "5th": 5, "last": -1, "final": -1}
NAME_STOPWORDS = {"on", "and", "with", "the", "a", "of"}
GENERIC_NAME_WORDS = {"simple", "spicy", "vegetable", "fried", "mashed"}   # too vague to identify a recipe globally


@dataclass
class Reference:
    kind: str            # name | partial | ordinal | pronoun | multiple | out_of_range |
                         # ordinal_no_suggestions | pronoun_ambiguous | pronoun_none | none
    recipe: dict | None = None
    candidates: list = field(default_factory=list)


def find_ordinal(text):
    """Position the user refers to (1-based, -1 = last), or None."""
    for pattern in ORDINAL_PATTERNS:
        m = pattern.search(text)
        if m:
            return WORD_TO_POSITION[m.group(1).lower()]
    for pattern in NUMBER_PATTERNS:
        m = pattern.search(text)
        if m:
            return int(m.group(1))
    return None


def _full_name_hits(text_norm, recipes):
    hits = [r for r in recipes if _phrase_in(text_norm, normalize(r["name"]))]
    # if one matched name sits inside another matched name, keep only the longer one
    return [r for r in hits
            if not any(o is not r and normalize(r["name"]) in normalize(o["name"]) and
                       len(normalize(o["name"])) > len(normalize(r["name"])) for o in hits)]


def _partial_hits(text, text_norm, recipes, suggested, ingredient_tokens, recipe_intent, strong):
    """Match on one distinctive word of a recipe name ('the paratha', 'the toast one')."""
    words = set(text_norm.split())
    for pool, is_suggested in ((suggested, True), (recipes, False)):
        if not pool:
            continue

        def tokens(r):
            return {t for t in normalize(r["name"]).split() if t not in NAME_STOPWORDS and t not in ingredient_tokens}

        counts = Counter(t for r in pool for t in tokens(r))
        hits = []
        for r in pool:
            for t in tokens(r):
                if counts[t] != 1 or len(t) < 3 or t not in words:
                    continue
                if not is_suggested and t in GENERIC_NAME_WORDS:
                    continue
                the_phrase = re.search(rf"\b(?:the|that|this) (?:\w+ )?{re.escape(t)}\b", text_norm) is not None
                ok = (the_phrase and (is_suggested or recipe_intent)) if strong else recipe_intent
                if ok:
                    hits.append(r)
                    break
        if hits:
            return hits
    return []


def resolve_reference(text, recipes, suggested=(), focus=None):
    suggested = list(suggested)
    text_norm = normalize(text)
    ingredient_tokens = {t for r in recipes for i in r["ingredients"] for t in normalize(i["name"]).split()}
    recipe_intent = _has_recipe_intent(text)

    hits = _full_name_hits(text_norm, recipes)                                  # 1. full recipe name
    if len(hits) == 1:
        return Reference("name", hits[0])
    if len(hits) > 1:
        return Reference("multiple", candidates=hits)

    position = find_ordinal(text)                                               # 2. "the second one"
    if position is not None:
        if not suggested:
            return Reference("ordinal_no_suggestions")
        index = len(suggested) if position == -1 else position
        if 1 <= index <= len(suggested):
            return Reference("ordinal", suggested[index - 1])
        return Reference("out_of_range", candidates=suggested)

    for strong in (True, False):
        if strong is False and recipe_intent and PRONOUN.search(text):          # 4. "it", "that one"
            if focus:
                return Reference("pronoun", focus)
            if len(suggested) == 1:
                return Reference("pronoun", suggested[0])
            if suggested:
                return Reference("pronoun_ambiguous", candidates=suggested)
            return Reference("pronoun_none")
        # 3. one word of a name. Strong ("the paratha") beats "it"; weak ("is it spicy?") does not.
        hits = _partial_hits(text, text_norm, recipes, suggested, ingredient_tokens, recipe_intent, strong)
        if len(hits) == 1:
            return Reference("partial", hits[0])
        if len(hits) > 1:
            return Reference("multiple", candidates=hits)
    return Reference("none")


# ------------------------------------------------------------------ the final decision
@dataclass
class Interpretation:
    intent: str                  # suggest | detail | followup | more | clarify | chat
    ingredients: list = field(default_factory=list)
    recipe: dict | None = None   # for detail / followup
    topic: str | None = None     # for followup: time | servings | ingredients | difficulty | other
    candidates: list = field(default_factory=list)   # for clarify
    reason: str = ""             # for clarify: multiple | out_of_range | nothing_suggested | ambiguous | no_context


def _about_recipe(text, ingredients, recipe):
    intent, topic = _classify_recipe_request(text)
    return Interpretation(intent, ingredients, recipe=recipe, topic=topic)


def interpret(text, found, recipes, suggested=(), focus=None):
    """found = ingredients already extracted from `text`.
    focus must only be passed while it is still the current topic (i.e. no newer suggestion list)."""
    suggested = list(suggested)
    ingredients = list(found)
    ref = resolve_reference(text, recipes, suggested, focus)

    if ref.kind in ("name", "partial", "ordinal"):
        return _about_recipe(text, ingredients, ref.recipe)
    if ref.kind == "multiple":
        return Interpretation("clarify", ingredients, candidates=ref.candidates, reason="multiple")
    if ref.kind == "out_of_range":
        return Interpretation("clarify", ingredients, candidates=ref.candidates, reason="out_of_range")
    if ref.kind == "ordinal_no_suggestions":
        return Interpretation("clarify", ingredients, reason="nothing_suggested")
    if ingredients and (MODIFY.search(text) or CONTENT_QUESTION.search(text)):
        if ref.kind == "pronoun":
            return _about_recipe(text, ingredients, ref.recipe)
        if ref.kind == "pronoun_ambiguous":
            return Interpretation("clarify", ingredients, candidates=ref.candidates, reason="ambiguous")
        if ref.kind == "pronoun_none":
            return Interpretation("clarify", ingredients, reason="no_context")
    if ingredients and GENERAL_COOKING.search(text) and not SEARCH_HINT.search(text):
        return Interpretation("chat", ingredients)       # a general cooking question, not a search
    if ingredients:
        return Interpretation("suggest", ingredients)
    if MORE.search(text):
        return Interpretation("more", ingredients)
    if ref.kind == "pronoun":
        return _about_recipe(text, ingredients, ref.recipe)
    if ref.kind == "pronoun_ambiguous":
        return Interpretation("clarify", ingredients, candidates=ref.candidates, reason="ambiguous")
    if ref.kind == "pronoun_none":
        return Interpretation("clarify", ingredients, reason="no_context")
    return Interpretation("chat", ingredients)


# ------------------------------------------------------------------ fixed wording for questions we must ask
def _join_names(recipes):
    names = [r["name"] for r in recipes]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


def clarification_text(interp):
    if interp.reason == "multiple":
        return f"I see more than one recipe in your message ({_join_names(interp.candidates)}). Which one would you like to start with?"
    if interp.reason == "out_of_range":
        n = len(interp.candidates)
        return f"I only suggested {n} dish{'' if n == 1 else 'es'}: {_join_names(interp.candidates)}. Which one would you like?"
    if interp.reason == "ambiguous":
        return f"Which one do you mean: {_join_names(interp.candidates)}?"
    if interp.reason == "nothing_suggested":
        return "I haven't suggested any dishes yet. Tell me which ingredients you have and I'll suggest some."
    return "Which recipe do you mean? Tell me its name, or tell me which ingredients you have and I'll suggest some."