"""Natural-language ingredient database additions without a mandatory pipe syntax.

All values come from the user. Partial entries are kept private to one Telegram user.
"""
from __future__ import annotations
import math
import re
from google_sheets import Ingredient, validate_ingredient

FIELDS = ("kcal", "protein", "fat", "carbs")
LABELS = {"kcal": "калорий (ккал)", "protein": "белков (г)",
          "fat": "жиров (г)", "carbs": "углеводов (г)"}
INTENT = re.compile(
    r"(?:\bingredient(?:s)?\b|\b(?:add|save|new) (?:a |an )?(?:food|product)\b"
    r"|(?:в|для)\s+(?:мою\s+)?баз[ыуе]\b|справочник|добавь продукт"
    r"|новый продукт|запиши продукт|сохрани продукт|база продуктов|новый ингредиент|добавь ингредиент|запиши ингредиент|внести в таблицу|внеси в базу|add to (?:the )?(?:sheet|database))",
    re.IGNORECASE,
)


def has_ingredient_intent(text: str) -> bool:
    return bool(INTENT.search(text or ""))


def combine(pending: dict | None, change) -> dict:
    data = dict(pending or {})
    fields = ("name", "kind", "basis_g", *FIELDS)
    for field in fields:
        value = getattr(change, field, None)
        if value is not None:
            data[field] = value
    aliases = getattr(change, "aliases", [])
    if aliases:
        data["aliases"] = [str(a).strip() for a in aliases if str(a).strip()]
    return data


def missing_fields(data: dict) -> list[str]:
    missing = []
    if not data.get("name"):
        missing.append("название продукта")
    if not data.get("basis_g"):
        missing.append("на какой вес указаны значения (например, на 100 г)")
    missing.extend(LABELS[field] for field in FIELDS if data.get(field) is None)
    return missing


def ready_ingredient(data: dict) -> tuple[str, Ingredient]:
    missing = missing_fields(data)
    if missing:
        raise ValueError("Не хватает: " + ", ".join(missing))
    basis = float(data["basis_g"])
    if not math.isfinite(basis) or basis <= 0 or basis > 100000:
        raise ValueError("Некорректный вес, к которому относятся КБЖУ.")
    name = str(data["name"]).strip()
    kind = data.get("kind") or ("ru" if re.search("[А-Яа-яЁё]", name) else "en")
    if kind not in ("ru","en","custom"):
        raise ValueError("Категория должна быть ru, en или custom.")
    values = []
    for macro in FIELDS:
        value = float(data[macro])
        if not math.isfinite(value) or value < 0:
            raise ValueError("Некорректное значение: " + macro)
        values.append(round(value * 100 / basis, 4))
    item = Ingredient(name, tuple(data.get("aliases", [])), *values)
    validate_ingredient(item)
    return kind, item


def prompt_for(data: dict) -> str:
    missing = missing_fields(data)
    if not missing:
        return ""
    name = data.get("name") or "нового продукта"
    return (f"Хорошо, добавим в базу {name}. "
            "Мне осталось уточнить: " + ", ".join(missing) + ".\n"
            "Можешь просто написать обычным текстом, например: "
            "«На 100 г: 75 ккал, белки 12 г, жиры 1,5 г, углеводы 3 г». "
            "Я ничего не буду додумывать.")
