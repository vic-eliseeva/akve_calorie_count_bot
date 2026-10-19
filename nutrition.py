"""Gemini extracts food names and weights from Russian/English text, photos and audio.

ALL nutritional figures are taken from the connected Google Sheet, not Gemini.
"""
from __future__ import annotations

import json
import math
from typing import Literal

from pydantic import BaseModel, Field
from google import genai
from google.genai import types


class FoodItem(BaseModel):
    name: str = Field(description='One consumed ingredient without weight included in name')
    portion_g: float | None = Field(default=None, description='Weight in grams; null if not confirmed')
    reference: str | None = Field(default=None, description='Exact Google Sheets ingredient name or alias, else null')
    lookup_query: str | None = Field(default=None, description='Short English singular name for a generic whole food not found in the sheet, e.g. banana or mango; null otherwise')
    simple_ingredient: bool = Field(default=False, description='True only for one basic unbranded, single-ingredient food, never a mixed prepared meal, recipe, flavored or branded product')
    source_url: str = ''
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


PROMPT = """Extract ingredient names and measured quantities for a Russian/English food diary.
NEVER calculate or invent kcal, protein, fat or carbohydrates. Numeric
nutrients come ONLY from the spreadsheet or verified USDA online lookup done by the application.
Identify EVERY ingredient separately, including oil, sauce and drinks.
- Google Sheet matches always take priority. For other simple, unbranded, ONE-ingredient foods
  such as raw banana or mango, set simple_ingredient=true and lookup_query to the
  exact generic English food name, e.g. "banana" or "mango".
- For any mixed dish (lasagna, restaurant food, stew, cake, smoothie, etc.), composite food,
  branded item, unclear variety/cooking state, or a guessed recipe: simple_ingredient=false.
  Split the dish into ingredients only if the user provided them. Never assume a recipe.
- lookup_query does not supply nutritional facts; the app independently queries USDA
  and accepts only sufficiently exact matches. If uncertain, ask the user instead.
- Ingredient name must exclude the weight; portion_g is grams or null if not known.
- Never invent weights, calorie values, ingredients or substitutes.
- Accept English/Russian text, voice, images and mixed-language messages.
- For photos without a known weight, return null grams and request clarification.
- Only set reference to an EXACT, confident Google Sheet name or alias; otherwise null.
  Distinguish cooked from raw weights, brands, and fat percentages.
- action=log if a meal was eaten or user explicitly uses /log; action=question for
  recipes and calculation requests; action=ignore for unrelated chat.
- For corrections return every ingredient (including unchanged ones), with original weights
  preserved except where the user explicitly changes them.
- Never claim food was saved. The app validates ALL ingredients before logging.
"""


class NutritionAI:
    def __init__(self, api_key: str, model: str):
        self.client = genai.Client(api_key=api_key)
        self.model = model

    def interpret(self, *, text: str = '', media: bytes | None = None, mime: str | None = None,
                  force_log: bool = False, correction: dict | None = None,
                  references: list | None = None) -> FoodResult:
        instruction = 'Extract all ingredient names and verified gram quantities.'
        if force_log:
            instruction += ' The user requests logging (only if complete and verified).'
        if correction:
            instruction += '\nOriginal saved meal: ' + json.dumps({
                'title': correction['title'],
                'items': json.loads(correction['ingredients_json']),
                'original_text': correction['source_text']}, ensure_ascii=False)
            instruction += '\nRequested correction: ' + text
        else:
            instruction += '\nIncoming message: ' + (text or '(audio or image without caption)')
        instruction += '\nEXACT known Google Sheet names and aliases: ' + json.dumps([
            {'name': item.name, 'aliases': list(item.aliases)}
            for item in (references or [])], ensure_ascii=False)
        contents = [instruction]
        if media is not None:
            contents.append(types.Part.from_bytes(data=media, mime_type=mime or 'application/octet-stream'))
        response = self.client.models.generate_content(
            model=self.model, contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=PROMPT, temperature=0.1,
                response_mime_type='application/json', response_schema=FoodResult))
        result = response.parsed
        if not isinstance(result, FoodResult):
            result = FoodResult.model_validate_json(response.text)
        if correction or force_log:
            if result.action == 'question' and result.items:
                result.action = 'log'
        # Provider-supplied nutrition data is never trustworthy, even if returned anyway.
        for item in result.items:
            item.kcal = item.protein = item.fat = item.carbs = 0
            item.per100_kcal = item.per100_protein = item.per100_fat = item.per100_carbs = None
            item.source_tab = ''
            item.source_url = ''
            if item.portion_g is not None and (not math.isfinite(item.portion_g) or item.portion_g <= 0):
                item.portion_g = None
        return result
