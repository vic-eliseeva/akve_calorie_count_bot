"""Offline test of multilingual/text/audio request plumbing with fake Gemini responses.

Not a live speech-recognition accuracy test: that requires the user's Gemini key.
"""
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Avoid importing the external SDK in the isolated test environment.
google = sys.modules.get('google') or types.ModuleType('google')
if not hasattr(google, '__path__'):
    google.__path__ = []
genai = types.ModuleType('google.genai')
types_mod = types.ModuleType('google.genai.types')
class FakePart:
    @staticmethod
    def from_bytes(data, mime_type):
        return dict(data=data, mime_type=mime_type)
class FakeConfig:
    def __init__(self, **kwargs):
        self.options = kwargs
types_mod.Part = FakePart
types_mod.GenerateContentConfig = FakeConfig
genai.types = types_mod
sys.modules['google'] = google
sys.modules['google.genai'] = genai
sys.modules['google.genai.types'] = types_mod
google.genai = genai

from nutrition import NutritionAI, FoodResult, FoodItem
from google_sheets import Ingredient, calculate_from_table


class RecordingGemini:
    def __init__(self):
        self.calls = []
    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return types.SimpleNamespace(parsed=FoodResult(
            action='log', title='Творог / Cottage cheese',
            items=[FoodItem(name='cottage cheese, 200g', kcal=400, protein=0, fat=0, carbs=0,
                            portion_g=200, reference='cottage cheese 1.5%')],
            assumptions='', confidence='high', answer=''))

class BilingualAIWiringTests(unittest.TestCase):
    def test_two_languages_and_ogg_audio_get_same_reference_macros(self):
        client = RecordingGemini()
        ai = NutritionAI.__new__(NutritionAI)
        ai.client = types.SimpleNamespace(models=client)
        ai.model = 'test-model'
        reference = [Ingredient('Творог 1.5%', ('cottage cheese 1.5%',), 73, 12, 1.5, 2.7)]
        for text, audio in [('Добавь 200 г творога 1.5%', None),
                            ('I ate 200g cottage cheese', None),
                            ('Russian and English in voice', b'fake-ogg-opus')]:
            response = ai.interpret(text=text, media=audio, mime='audio/ogg' if audio else None,
                                    references=reference)
            # The AI may extract the food identity/weight, but it must never set macros.
            self.assertEqual((response.items[0].kcal, response.items[0].protein), (0, 0))
            calculate_from_table(response, reference)
            self.assertEqual((response.items[0].kcal, response.items[0].protein,
                              response.items[0].fat, response.items[0].carbs), (146, 24, 3, 5.4))
        self.assertEqual(len(client.calls), 3)
        self.assertIn('Творог 1.5%', client.calls[0]['contents'][0])
        self.assertIn('cottage cheese 1.5%', client.calls[1]['contents'][0])
        self.assertEqual(client.calls[2]['contents'][1], {'data': b'fake-ogg-opus', 'mime_type': 'audio/ogg'})
        self.assertIn('русском', client.calls[0]['config'].options['system_instruction'])

if __name__ == '__main__':
    unittest.main()
