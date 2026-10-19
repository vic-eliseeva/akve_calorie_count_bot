"""Multimodal Gemini nutrition estimation; never used as a medical/nutrition database."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field
from google import genai
from google.genai import types


class FoodItem(BaseModel):
    name: str = Field(description="Ingredient or food with the portion included in the name")
    kcal: float = Field(ge=0)
    protein: float = Field(ge=0, description="Grams of protein")
    fat: float = Field(ge=0, description="Grams of fat")
    carbs: float = Field(ge=0, description="Grams of carbohydrates")
    portion_g: float | None = Field(default=None, ge=0, description="Weight in grams of this specific food (not the whole meal)")
    reference: str | None = Field(default=None, description="Canonical ingredient name from the provided Google Sheet or null")


class FoodResult(BaseModel):
    action: Literal['log', 'question', 'ignore']
    title: str = Field(description="Short descriptive meal title in input language")
    items: list[FoodItem] = Field(default_factory=list)
    assumptions: str = Field(description="Brief uncertainties such as unknown quantities/oils")
    confidence: Literal['high', 'medium', 'low']
    answer: str = Field(description="For action=question, answer succinctly in the user's language; otherwise empty")


PROMPT = """You are a precise bilingual (Russian/English) food diary nutrition interpreter for a private
Telegram group. Read food descriptions, photos, or voice in Russian or English and calculate nutrition.
Use common reference nutrient values; nutrition values are estimates, not lab measurements.
Rules:
- action='log' ONLY when user clearly says they ATE/DRANK a food, or commands you to log it.
- action='question' when user asks for a calorie estimate or describes a batch recipe without saying
  they ate it. Provide calculations in answer but DO NOT add to the diary.
- action='ignore' for ordinary chat or unrelated content; do not answer.
- If photo has no caption, default to 'log' when it depicts food; flag portions as visual estimates.
- Include separate line items for ALL foods with stated quantities. Use grams, not implied servings,
  and accurately distinguish uncooked from cooked quantities.
- Respect branded nutrition labels and user-stated nutrition values over generic database estimates.
- Count oils, sauces, drinks and additions if specified. If not specified, don't invent exact oil weights;
  mention what you assumed, and put confidence low for ambiguous portions.
- If user describes a whole recipe, do NOT log the whole batch as a meal unless explicitly eaten.
- Never hallucinate that 'skyr' or substitute ingredients were used when user did not mention them.
- Every FoodItem's macros correspond to that item's total portion, NOT values per 100g.
- Calories should be consistent with macronutrients when possible, allowing for fiber and rounding.
- For a meal with a precise recipe, calculate using all quantities, no unexplained generic item.
- For corrections, use the provided ORIGINAL MEAL details, change ONLY what is requested and
  recompute the full meal. Return action='log'.
- Interpret text and Telegram audio in either Russian or English, including mixed-language notes.
- When GOOGLE SHEET REFERENCE INGREDIENTS are supplied, match food names in Russian or English
  via their exact names and aliases. Set `reference` to the canonical ingredient name, and
  `portion_g` to the weight for that ingredient only. Preserve the dry/cooked distinction.
  The application calculates its nutrition exactly from the Google Sheet per-100g row.
- If there is no confident reference match, set reference=null and estimate normally.
- Never invent a known ingredient match for unrelated items or unknown portion sizes.
- Respond using user's language for meal title, assumptions and answer.
- No health, dieting or weight-loss coaching; nutrition bookkeeping only.
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
            ingredients = [{'name': i.name, 'aliases': list(i.aliases), 'kcal': i.kcal,
                            'protein': i.protein, 'fat': i.fat, 'carbs': i.carbs}
                           for i in references]
            instruction += "\nGOOGLE SHEET REFERENCE INGREDIENTS, macros per 100g: " + json.dumps(ingredients, ensure_ascii=False)
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
        if references:
            from google_sheets import apply_reference_macros
            apply_reference_macros(result, references)
        return result
