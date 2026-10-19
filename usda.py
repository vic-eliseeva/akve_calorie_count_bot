"""Verified online nutrient lookup. Never use language-model memory for macros.

USDA FoodData Central generic whole-food references, per 100 g. Search responses
are treated as untrusted: only a conservative exact single-food match is accepted.
"""
from __future__ import annotations

import os
import re
import logging
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)
BASE_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"
BLOCKED = {
    "cake", "pie", "soup", "stew", "casserole", "salad", "sandwich",
    "pizza", "pasta", "burger", "burrito", "lasagna", "smoothie",
    "sauce", "dressing", "meal", "dish", "curry", "porridge",
    "bread", "cookie", "muffin", "pancake", "wrap", "taco",
    "chocolate", "dessert", "mix", "recipe", "shake", "fried",
}
ALTERED = {
    "dried", "dehydrated", "canned", "sweetened", "syrup", "juice",
    "concentrate", "powder", "flour", "frozen", "steamed", "boiled",
    "baked", "roasted", "grilled", "cooked", "microwaved", "smoked",
}
NUTRIENTS = {"kcal": (1008, 2047, 2048), "protein": (1003,),
             "fat": (1004,), "carbs": (1005,)}


def _singular(s: str) -> str:
    s = s.lower().strip().replace("mangoes", "mangos")
    if s.endswith("ies") and len(s) > 4:
        return s[:-3] + "y"
    if s.endswith("oes") and len(s) > 4:
        return s[:-2]
    if s.endswith("s") and not s.endswith(("ss", "us")) and len(s) > 3:
        return s[:-1]
    return s


@dataclass(frozen=True)
class PublicFood:
    name: str
    kcal: float
    protein: float
    fat: float
    carbs: float
    fdc_id: int
    description: str
    data_type: str

    @property
    def url(self) -> str:
        return f"https://fdc.nal.usda.gov/food-details/{self.fdc_id}/nutrients"


def _nutrients(food: dict) -> dict | None:
    found: dict[int, float] = {}
    for n in food.get("foodNutrients", []):
        number = n.get("nutrientNumber") or n.get("number") or n.get("nutrientId")
        try:
            code = int(number)
            amount = float(n.get("value", n.get("amount")))
        except (ValueError, TypeError):
            continue
        unit = str(n.get("unitName", "")).lower()
        if code in (1008, 2047, 2048) and unit not in ("kcal", ""):
            continue
        found[code] = amount
    result = {}
    for macro, codes in NUTRIENTS.items():
        match = next((found[k] for k in codes if k in found), None)
        if match is None:
            return None
        result[macro] = match
    if (not 0 < result["kcal"] <= 1000 or
        any(not 0 <= result[k] <= 100 for k in ("protein", "fat", "carbs"))):
        return None
    return result


def _match_score(query: str, food: dict) -> int | None:
    description = str(food.get("description", "")).lower().strip()
    parts = [p.strip() for p in description.split(",")]
    base = _singular(parts[0])
    tokens = query.lower().split()
    if not tokens or len(tokens) > 4 or any(t in BLOCKED for t in tokens):
        return None
    request_state = next((t for t in tokens if t in ALTERED or t == "raw"), None)
    core = [_singular(t) for t in tokens if t not in ALTERED and t != "raw"]
    # Do not silently substitute an unfamiliar type, blend, brand or prepared dish.
    if len(core) != 1 or core[0] != base:
        return None
    other = set(re.findall(r"[a-z]+", " ".join(parts[1:])))
    if other & BLOCKED:
        return None
    forbidden = (other & ALTERED) - ({request_state} if request_state else set())
    if forbidden:
        return None
    if request_state and request_state not in other:
        return None
    # Without state, the safest generic ingredient is raw, not a cooked recipe.
    score = 100
    if "raw" in other:
        score += 30
    elif not request_state:
        score -= 15
    if "with skin" in description or "fresh" in other:
        score += 2
    score -= max(0, len(parts) - 2) * 5
    if food.get("dataType") == "SR Legacy":
        score += 8
    return score


class USDAFoodLookup:
    def __init__(self, api_key: str | None = None, session=None):
        self.api_key = api_key or os.getenv("FDC_API_KEY", "DEMO_KEY")
        self.session = session if session is not None else requests.Session()
        self.cache: dict[str, PublicFood | None] = {}

    def lookup(self, query: str) -> PublicFood | None:
        query = (query or "").strip().lower()
        if not re.fullmatch(r"[a-z][a-z -]{1,55}", query):
            return None
        if query in self.cache:
            return self.cache[query]
        try:
            response = self.session.get(
                BASE_URL,
                params={"api_key": self.api_key, "query": query,
                        "dataType": "SR Legacy,Foundation", "pageSize": 25},
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            log.warning("USDA lookup unavailable for %s: %s", query, type(exc).__name__)
            return None
        options = []
        for record in data.get("foods", []):
            score = _match_score(query, record)
            values = _nutrients(record)
            if score is None or values is None:
                continue
            try:
                ident = int(record["fdcId"])
            except (KeyError, ValueError, TypeError):
                continue
            options.append((score, PublicFood(name=query, fdc_id=ident,
                         description=str(record["description"]),
                         data_type=str(record["dataType"]), **values)))
        options.sort(key=lambda x: x[0], reverse=True)
        chosen = options[0][1] if options and (
            len(options) == 1 or options[0][0] - options[1][0] >= 8
            or options[0][1].description == options[1][1].description
        ) else None
        self.cache[query] = chosen
        return chosen

    def resolve(self, item):
        """Used only after an exact user-spreadsheet lookup fails."""
        if not getattr(item, "simple_ingredient", False):
            return None
        query = getattr(item, "lookup_query", None) or ""
        return self.lookup(query)
