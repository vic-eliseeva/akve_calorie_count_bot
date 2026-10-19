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
    weight_estimated: bool = False
    estimate_note: str = ''
    estimated_per100_kcal: float | None = None
    estimated_per100_protein: float | None = None
    estimated_per100_fat: float | None = None
    estimated_per100_carbs: float | None = None
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
    restaurant_estimate: bool = False


PROMPT = """Ты русскоязычный ассистент дневника питания. Распознавай текст, голос,
фото на русском, английском и смешанном языке. ВСЕ ответы и названия — на русском.
Распознавай фразы «добавь мне», «вот мой обед», «here is my lunch», «I ate»
как съеденную еду, action=log. Вопрос о калориях или целом рецепте без
упоминания еды — action=question; посторонняя беседа — action=ignore.
Извлекай КАЖДЫЙ ингредиент отдельно, масла, соусы и напитки, если известны.
Для простого неизвестного продукта (банан, манго) укажи simple_ingredient=true
и краткий английский lookup_query. Точное совпадение в таблице: reference.
НЕ придумывай макросы обычных ингредиентов — их считает приложение.

Для ФОТО еды, ресторанного/столового обеда, либо явной просьбы о ПРИМЕРНОЙ
оценке разрешена визуальная/контекстная оценка граммов: weight_estimated=true,
estimate_note поясняет оценку. restaurant_estimate=true только для явно
описанного кафе/столовой/ресторанного блюда или явной просьбы прикинуть.
Если состав различим, разделяй компоненты без двойного учёта. Если блюдо
НЕРАЗДЕЛИМОЕ и нет точного справочного значения, можешь поставить
estimated_per100_kcal/protein/fat/carbs как ГРУБУЮ ИИ-оценку на 100г,
estimate_note ОБЯЗАТЕЛЬНО объясняет неопределённость. Для базовых продуктов
не ставь estimated_per100, используй справочник или USDA через приложение.
Не выдумывай скрытые ингредиенты, невидимые добавки или точность измерения.
Если блюдо непонятно — проси пояснение. Фото без подписи с едой обычно log.
Для обычных домашних текстовых блюд без просьбы об оценке или кафе неизвестный
рецепт не оценивай, уточняй состав. Для коррекций сохрани неизменённые позиции.
Никогда не утверждай, что запись уже сохранена.
"""

class NutritionAI:
    def __init__(self, api_key: str, model: str):
        self.client = genai.Client(api_key=api_key)
        self.model = model

    def interpret(self, *, text: str = '', media: bytes | None = None, mime: str | None = None,
                  force_log: bool = False, correction: dict | None = None,
                  references: list | None = None, photo: bool = False) -> FoodResult:
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
        if photo:
            instruction += '\nЭто фото: оцени видимые граммы, пометь вес как приблизительный.'
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
            if not (photo or result.restaurant_estimate):
                item.estimated_per100_kcal = item.estimated_per100_protein = None
                item.estimated_per100_fat = item.estimated_per100_carbs = None
                item.weight_estimated = False
                item.estimate_note = ''
            if photo and item.portion_g is not None:
                item.weight_estimated = True
            if item.portion_g is not None and (not math.isfinite(item.portion_g) or item.portion_g <= 0):
                item.portion_g = None
        return result
