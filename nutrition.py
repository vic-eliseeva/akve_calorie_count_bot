"""Multimodal Gemini nutrition estimation; never used as a medical/nutrition database."""

from __future__ import annotations

import json
import math
from typing import Literal

from pydantic import BaseModel, Field
from google import genai
from google.genai import types


class FoodItem(BaseModel):
    name: str = Field(description="One ingredient name, without the portion weight")
    portion_g: float | None = Field(default=None, description="Known weight in grams; unknown -> null")
    reference: str | None = Field(default=None, description="Exact name or alias from provided ingredient list, otherwise null")
    # Application-owned values, never trusted from AI.
    kcal: float = 0
    protein: float = 0
    fat: float = 0
    carbs: float = 0
    per100_kcal: float | None = None
    per100_protein: float | None = None
    per100_fat: float | None = None
    per100_carbs: float | None = None
    source_tab: str = ''


class FoodResult(BaseModel):
    action: Literal['log', 'question', 'ignore']
    title: str = ''
    items: list[FoodItem] = Field(default_factory=list)
    assumptions: str = ''
    confidence: Literal['high', 'medium', 'low'] = 'medium'
    answer: str = ''


PROMPT = """You extract food ingredient names and consumed weights for two people sharing
a Russian/English Telegram food diary. You DO NOT calculate calorie or macro values.
Only the authoritative Google Sheet has calories, protein, fat, and carbohydrates.
Extract EACH ingredient separately, including oil, sauces, drinks, and additions.
Do not invent ingredients, weights, nutritional values or assumptions.
- Ingredient name is without grams; portion_g is the known weight in grams.
- If the quantity is unknown, portion_g=null. Photos alone cannot accurately weigh food.
- Only match a name to the explicitly provided ingredient reference list. For exact,
  confident matching, set reference to canonical name or alias; otherwise null.
- Be careful with raw versus cooked weights. Never guess oil amounts.
- action=log only when food was actually eaten/drunk or /log is explicitly used.
- action=question for recipes, hypothetical food, macro calculations or estimation requests.
- action=ignore for messages that are not about food or meals.
- When correcting a meal, return ALL ingredients with corrected amounts; retain unchanged items.
- Handle both Russian and English text and voice, including mixed-language messages.
- Respond in the user's language; don't assert that a meal was saved.
"""

class NutritionAI:
    def __init__(self, api_key: str, model: str):
        self.client = genai.Client(api_key=api_key)
        self.model = model

    def interpret(self, *, text: str = '', media: bytes | None = None, mime: str | None = None,
                  force_log: bool = False, correction: dict | None = None,
                  references: list | None = None) -> FoodResult:
        instruction = "Analyze this incoming Telegram food diary message."
        if force_log:
            instruction += " User explicitly used /log: log this meal unless no edible content can be found."
        if correction:
            instruction = "REVISE this existing meal to reflect the correction. Keep unaffected foods and quantities. "
            instruction += "Existing meal JSON: " + json.dumps({
                'title': correction['title'],
                'items': json.loads(correction['ingredients_json']),
                'assumptions': correction['assumptions'],
                'original_text': correction['source_text'],
            }, ensure_ascii=False)
            instruction += "\nRequested correction: " + text
        else:
            instruction += "\nText or photo caption: " + (text or '(no text)')
        if references:
            ingredients = [{'name': i.name, 'aliases': list(i.aliases)} for i in references]
            instruction += "\nGOOGLE SHEET REFERENCE INGREDIENT NAMES/ALIASES ONLY: " + json.dumps(ingredients, ensure_ascii=False)
        contents: list = [instruction]
        if media is not None:
            contents.append(types.Part.from_bytes(data=media, mime_type=mime or 'application/octet-stream'))
        response = self.client.models.generate_content(
            model=self.model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=PROMPT,
                response_mime_type='application/json',
                response_schema=FoodResult,
                temperature=0.1,
            ),
        )
        result = response.parsed
        if not isinstance(result, FoodResult):
            result = FoodResult.model_validate_json(response.text)
        if correction or force_log:
            if result.action == 'question' and result.items:
                result.action = 'log'
        # Completely discard any nutritional numbers the AI may have generated.
        for item in result.items:
            item.kcal = item.protein = item.fat = item.carbs = 0
            item.per100_kcal = item.per100_protein = item.per100_fat = item.per100_carbs = None
            item.source_tab = ''
            if item.portion_g is not None and (not math.isfinite(item.portion_g) or item.portion_g <= 0):
                item.portion_g = None
        return result
