"""Offline end-to-end tests of Google Sheets requests and daily exports."""
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database import FoodDB
from google_sheets import GoogleSheets, Ingredient, SheetsError, apply_reference_macros, build_lookup, normalize_name
from types import SimpleNamespace


class FakeResponse:
    def __init__(self, data, code=200):
        self.data = data
        self.status_code = code
        self.ok = 200 <= code < 300
        self.content = json.dumps(data).encode()
        self.text = self.content.decode()
    def json(self):
        return self.data


class FakeGoogleSheetsAPI:
    def __init__(self):
        self.tabs = {'Our existing budget': [['This is our unrelated tab'], ['Do not change']]}
        self.actions = []

    def request(self, method, url, params=None, json=None, timeout=20):
        self.actions.append((method, url, params, json))
        path = url.split('/spreadsheets/', 1)[1].split('/', 1)
        suffix = '/' + path[1] if len(path) > 1 else ''
        if suffix == '' and method == 'GET':
            return FakeResponse({'sheets': [{'properties': {'title': title}} for title in self.tabs]})
        if path[0].endswith(':batchUpdate') and method == 'POST':
            tab = json['requests'][0]['addSheet']['properties']['title']
            self.tabs[tab] = []
            return FakeResponse({})
        if suffix.startswith('/values/'):
            rest = unquote(suffix[len('/values/'):])
            append = rest.endswith(':append')
            if append:
                rest = rest[:-7]
            match = re.fullmatch(r"'((?:[^']|'')+)'!(.*)", rest)
            assert match, rest
            tab = match.group(1).replace("''", "'")
            rng = match.group(2)
            if append and method == 'POST':
                self.tabs[tab].extend(json['values'])
                return FakeResponse({'updates': {'updatedRows': len(json['values'])}})
            if method == 'PUT':
                m = re.search(r'A(\d+):I\d+', rng)
                index = int(m.group(1)) - 1
                while len(self.tabs[tab]) <= index:
                    self.tabs[tab].append([])
                self.tabs[tab][index] = json['values'][0]
                return FakeResponse({'updatedRows': 1})
            if method == 'GET':
                return FakeResponse({'values': [row[:] for row in self.tabs[tab]]})
        return FakeResponse({'error': 'unsupported fake request: '+method+' '+suffix}, 400)


class GoogleSheetsTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGoogleSheetsAPI()
        self.gs = GoogleSheets('fake_google_sheet_id_123456789', transport=self.fake)
        self.gs.ensure_tabs()
        self.fake.tabs['Ingredients base'] = [['type','name','kcal','p','f','c'], ['Bread','Bagel',289,11,1.7,56]]
        self.fake.tabs['База ингредиентов'] = [['тип','название','kcal','p','f','c'], ['Хлеб','Бублик',289,11,1.7,56], ['', 'Onion', '', 'Лук',40,1.1,0.1,9.3]]
        self.fake.tabs['База продуктов'] = [['notes','name','kcal','p','f','c','fiber','check'], ['','Скир, Скюр',57,10,0.2,3.5,'','=formula']]
        self.fake.tabs['Ingredients base'] = [
            ['Product type','Product','Calories (kcal)','Protein (g)','Fat (g)','Carbs (g)'],
            ['Bread','Bagel',289,11,1.7,56]]
        self.fake.tabs['База ингредиентов'] = [
            ['Тип продукта','Продукт','Калории (ккал)','Белки (г)','Жиры (г)','Углеводы (г)'],
            ['Хлеб','Бублик',289,11,1.7,56]]
        self.fake.tabs['База продуктов'] = [
            ['Комментарии','Название','Калории (ккал)','Белки (г)','Жиры (г)','Углеводы (г)','Клетчатка (г)','Проверка'],
            ['Натурель','Скир, Скюр обычный',57,10,0.2,3.5,'','=formula'],
            ['', 'Skip incomplete reference', '', '', '', '', '', '']]


    def test_existing_tab_is_preserved(self):
        self.assertEqual(self.fake.tabs['Our existing budget'], [['This is our unrelated tab'], ['Do not change']])
        self.assertIn('Bot Ingredients', self.fake.tabs)
        self.assertIn('Bot Daily Totals', self.fake.tabs)
        self.gs.ensure_tabs()
        self.assertEqual(len(self.fake.tabs['Bot Ingredients']), 1)
        self.assertEqual(len(self.fake.tabs['Bot Daily Totals']), 1)

    def test_bilingual_alias_and_upsert_dont_duplicate(self):
        ing = Ingredient('Творог 1.5%', ('cottage cheese 1.5%', 'Quark 1.5%'), 73, 12, 1.5, 2.7)
        self.assertEqual(self.gs.upsert_ingredient(ing), 'added')
        references = self.gs.ingredients()
        self.assertEqual(build_lookup(references)[normalize_name('cottage cheese 1.5%')].kcal, 73)
        self.assertEqual(build_lookup(references)[normalize_name('ТВОРОГ 1,5%')].kcal, 73)
        self.assertEqual(self.gs.upsert_ingredient(Ingredient('Творог 1.5%', ing.aliases, 75, 13, 1.5, 2.7)), 'updated')
        self.assertEqual(len(self.gs.ingredients()), 5)
        self.assertEqual(self.gs.ingredients()[0].kcal, 75)
        with self.assertRaises(ValueError):
            self.gs.upsert_ingredient(Ingredient('Different', ('cottage cheese 1.5%',), 80, 12, 3, 2))
        self.assertEqual(build_lookup(self.gs.ingredients())['скюр обычный'].kcal, 57)
        self.assertEqual(self.fake.tabs['База продуктов'][1][1], 'Скир, Скюр обычный')

    def test_actual_per_100g_math_overrides_ai_result(self):
        ref = Ingredient('Творог 1.5%', ('cottage cheese 1.5%',), 73, 12, 1.5, 2.7)
        response = SimpleNamespace(items=[
            SimpleNamespace(name='200g cottage cheese', kcal=999, protein=999, fat=99, carbs=99,
                            portion_g=200, reference='cottage cheese 1.5%'),
            SimpleNamespace(name='2 eggs (estimate)', kcal=143, protein=13, fat=10, carbs=1,
                            portion_g=None, reference=None)])
        apply_reference_macros(response, [ref])
        self.assertEqual([response.items[0].kcal, response.items[0].protein,
                          response.items[0].fat, response.items[0].carbs], [146, 24, 3, 5.4])
        self.assertEqual(response.items[1].kcal, 143)

    def test_daily_upsert_twice_updates_not_appends(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = FoodDB(str(Path(tmp) / 'db.sqlite'))
            chat = -1234
            db.join(chat, 111, 'Вика', '2026-10-17T10:00:00+02:00')
            db.join(chat, 222, 'Tolya', '2026-10-17T10:01:00+02:00')
            db.add_meal(chat_id=chat, user_id=111, day='2026-10-17', now='2026-10-17T10:00:00+02:00',
                        title='Творог', source_text='Творог 200 г', source_kind='voice',
                        items=[dict(name='Творог 200г', kcal=146, protein=24, fat=3, carbs=5.4)],
                        assumptions='', confidence='high', source_message_id=1)
            db.add_meal(chat_id=chat, user_id=222, day='2026-10-17', now='2026-10-17T10:05:00+02:00',
                        title='Eggs', source_text='2 eggs', source_kind='text',
                        items=[dict(name='Eggs', kcal=140, protein=12, fat=10, carbs=1)],
                        assumptions='', confidence='high', source_message_id=2)
            self.assertEqual(self.gs.upsert_day('2026-10-17', db.members(chat), db, chat), 2)
            self.assertEqual(len(self.fake.tabs['Bot Daily Totals']), 3)
            self.assertEqual(self.fake.tabs['Bot Daily Totals'][1][:7], ['2026-10-17', '111', 'Вика', 146, 24, 3, 5.4])
            self.assertEqual(self.gs.upsert_day('2026-10-17', db.members(chat), db, chat), 2)
            self.assertEqual(len(self.fake.tabs['Bot Daily Totals']), 3)
            meal = db.last(chat, 111)
            db.update_meal(chat, 111, meal['id'], 'Corrected', '200g', [dict(name='food', kcal=220, protein=32, fat=5, carbs=12)], '', 'high')
            self.gs.upsert_day('2026-10-17', db.members(chat), db, chat)
            self.assertEqual(self.fake.tabs['Bot Daily Totals'][1][3:7], [220, 32, 5, 12])
            self.assertEqual(len(self.fake.tabs['Bot Daily Totals']), 3)

    def test_existing_conflicting_tab_fails_without_overwriting(self):
        fake = FakeGoogleSheetsAPI()
        fake.tabs['Bot Ingredients'] = [['My personal notes']]
        gs = GoogleSheets('fake_google_sheet_id_123456789', transport=fake)
        with self.assertRaises(SheetsError):
            gs.ensure_tabs()
        self.assertEqual(fake.tabs['Bot Ingredients'], [['My personal notes']])


if __name__ == '__main__':
    unittest.main()
