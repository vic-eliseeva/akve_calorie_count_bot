"""Offline tests for spreadsheet-only macros, write destinations and daily totals."""
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database import FoodDB
from google_sheets import GoogleSheets, Ingredient, MissingReferences, calculate_from_table, build_lookup, normalize_name

class FakeResponse:
    def __init__(self, data):
        self.ok = True
        self.status_code = 200
        self.data = data
        self.content = json.dumps(data).encode()
        self.text = self.content.decode()
    def json(self):
        return self.data

class FakeSheets:
    def __init__(self):
        self.tabs = {
            'Bot Daily Totals': [['Date', 'Telegram_user_id', 'Name', 'kcal', 'protein_g', 'fat_g', 'carbs_g', 'Meals_count', 'Last_synced']],
            'База продуктов': [
                ['Комментарии','Название','Калории','Белки','Жиры','Углеводы','Клетчатка','Проверка'],
                ['Natural','Скир, Скюр обычный',57,10,0.2,3.5,'','=FORMULA']],
            'База ингредиентов': [
                ['Тип','Продукт','Калории','Белки','Жиры','Углеводы'],
                ['Молочное','Творог 1.5%',73,12,1.5,2.7],
                ['', 'Burrito mix', '', 'буррито специи',290,12,6.5,38]],
            'Ingredients base': [
                ['Type','Product','Calories','Protein','Fat','Carbs'],
                ['Bread','Bagel',289,11,1.7,56]]
        }
        self.calls = []

    def request(self, method, url, params=None, json=None, timeout=None):
        self.calls.append((method, url, params, json))
        rest = url.split('/spreadsheets/', 1)[1].split('/', 1)
        suffix = '/' + rest[1] if len(rest)>1 else ''
        if method == 'GET' and not suffix:
            return FakeResponse({'sheets':[{'properties':{'title':t}} for t in self.tabs]})
        if suffix.startswith('/values/'):
            path = unquote(suffix[len('/values/'):])
            append = path.endswith(':append')
            path = path.removesuffix(':append')
            match = re.fullmatch(r"'((?:[^']|'')+)'!(.*)", path)
            assert match is not None, path
            tab = match.group(1).replace("''", "'")
            if method == 'GET':
                return FakeResponse({'values':[r[:] for r in self.tabs[tab]]})
            if method == 'POST' and append:
                self.tabs[tab].append(json['values'][0])
                return FakeResponse({'updates': {'updatedRows': 1}})
            if method == 'PUT':
                row_idx = int(re.search(r'A(\d+):', match.group(2)).group(1))-1
                while len(self.tabs[tab]) <= row_idx:
                    self.tabs[tab].append([])
                self.tabs[tab][row_idx] = json['values'][0]
                return FakeResponse({'updatedRows': 1})
        raise AssertionError(f'Unexpected Google API operation: {method}, {suffix}')

def make_item(name, grams, reference=None, fabricated=777777):
    return SimpleNamespace(name=name, reference=reference, portion_g=grams,
                           kcal=fabricated, protein=fabricated,
                           fat=fabricated, carbs=fabricated,
                           source_tab='', per100_kcal=None, per100_protein=None,
                           per100_fat=None, per100_carbs=None)

class ReferenceOnlyTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeSheets()
        self.gs = GoogleSheets('test_spreadsheet_id_123456', transport=self.api)

    def test_read_original_tabs_without_mutation(self):
        import copy
        before = copy.deepcopy(self.api.tabs)
        self.gs.ensure_tabs()
        refs = self.gs.ingredients()
        lookup = build_lookup(refs)
        self.assertEqual(lookup[normalize_name('СКЮР ОБЫЧНЫЙ')].kcal, 57)
        self.assertEqual(lookup[normalize_name('БУРРИТО СПЕЦИИ')].protein, 12)
        self.assertEqual(lookup['bagel'].kcal, 289)
        self.assertEqual(self.api.tabs, before)
        self.assertNotIn('Bot Ingredients', str(self.api.calls))

    def test_all_macros_from_sheet_even_if_ai_returns_fake_numbers(self):
        item = make_item('Творог 1.5%', 200, reference='Творог 1.5%')
        calculate_from_table(SimpleNamespace(items=[item]), self.gs.ingredients())
        self.assertEqual((item.kcal,item.protein,item.fat,item.carbs),(146,24,3,5.4))
        self.assertEqual((item.per100_kcal, item.per100_protein), (73,12))
        self.assertEqual(item.source_tab, 'База ингредиентов')

    def test_entire_meal_blocked_by_unknown_ingredient(self):
        known = make_item('Bagel',100,'Bagel')
        unknown = make_item('Mystery ingredient', 20, 'invented-food')
        with self.assertRaises(MissingReferences) as caught:
            calculate_from_table(SimpleNamespace(items=[known,unknown]),self.gs.ingredients())
        self.assertIn('Mystery ingredient', caught.exception.missing)

    def test_unknown_weight_blocks_meal(self):
        item = make_item('Bagel', None, 'Bagel')
        with self.assertRaises(MissingReferences) as caught:
            calculate_from_table(SimpleNamespace(items=[item]), self.gs.ingredients())
        self.assertIn('Bagel', caught.exception.missing_weights)

    def test_new_products_append_to_three_named_tabs(self):
        self.assertEqual(self.gs.add_ingredient('en',Ingredient('New pasta',(),302,11,2,62)),'Ingredients base')
        self.assertEqual(self.api.tabs['Ingredients base'][-1],['Added from Telegram','New pasta',302,11,2,62])
        self.assertEqual(self.gs.add_ingredient('ru',Ingredient('Новая паста',(),302,11,2,62)),'База ингредиентов')
        self.assertEqual(self.gs.add_ingredient('custom',Ingredient('Custom bread',('Alias bread',),250,8,1.2,50)),'База продуктов')
        self.assertEqual(self.api.tabs['База продуктов'][-1][1],'Custom bread, Alias bread')
        with self.assertRaises(ValueError):
            self.gs.add_ingredient('en',Ingredient('Bagel',(),320,10,3,60))
        self.assertEqual(self.api.tabs['База продуктов'][1][-1], '=FORMULA')

    def test_daily_results_update_same_person_day_without_duplicates(self):
        with tempfile.TemporaryDirectory() as temp:
            db=FoodDB(str(Path(temp)/'db.sqlite3'))
            db.join(-222,111,'Vika','2026-10-19T09:00:00+02:00')
            db.join(-222,222,'Tolya','2026-10-19T09:05:00+02:00')
            db.add_meal(chat_id=-222,user_id=111,day='2026-10-19',
                now='2026-10-19T10:00:00+02:00',title='Bagel',source_text='Bagel',
                source_kind='text',items=[{'name':'Bagel','kcal':289,'protein':11,'fat':1.7,'carbs':56}],
                assumptions='',confidence='high',source_message_id=1)
            members=db.members(-222)
            self.gs.upsert_day('2026-10-19',members,db,-222)
            self.gs.upsert_day('2026-10-19',members,db,-222)
            self.assertEqual(len(self.api.tabs['Bot Daily Totals']),3)
            self.assertEqual(self.api.tabs['Bot Daily Totals'][1][3:7],[289,11,1.7,56])
            self.assertEqual(self.api.tabs['Bot Daily Totals'][2][3],0)

if __name__ == '__main__':
    unittest.main()
