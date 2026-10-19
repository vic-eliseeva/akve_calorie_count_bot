"""Tests for conversational ingredient intake and explicitly uncertain restaurant photos."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from database import FoodDB
from ingredient_chat import has_ingredient_intent, combine, ready_ingredient, prompt_for
from google_sheets import Ingredient, MissingReferences, calculate_from_table


def item(name, grams, *, estimate=False, values=None, weight_estimated=False):
    vals = values or (None, None, None, None)
    return SimpleNamespace(
        name=name, reference=None, portion_g=grams, lookup_query=None,
        simple_ingredient=False, weight_estimated=weight_estimated,
        estimated_per100_kcal=vals[0], estimated_per100_protein=vals[1],
        estimated_per100_fat=vals[2], estimated_per100_carbs=vals[3],
        estimate_note='Оценка по видимому объёму тарелки' if estimate else '',
        per100_kcal=None, per100_protein=None, per100_fat=None, per100_carbs=None,
        source_tab='', source_url='', kcal=99999, protein=999, fat=999, carbs=999,
    )


class ConversationTests(unittest.TestCase):
    def test_add_to_database_not_confused_with_log(self):
        self.assertTrue(has_ingredient_intent('Добавь в базу творог 1.5%'))
        self.assertTrue(has_ingredient_intent('Save ingredient skyr'))
        self.assertFalse(has_ingredient_intent('Добавь мне 100 г банана'))
        self.assertFalse(has_ingredient_intent('Вот мой обед'))

    def test_unstructured_two_messages_per100(self):
        pending=combine(None,SimpleNamespace(name='Творог',kind='ru',
            kcal=None,protein=None,fat=None,carbs=None,basis_g=None,aliases=[]))
        self.assertIn('на какой вес',prompt_for(pending))
        data=combine(pending,SimpleNamespace(name=None,kind=None,
            kcal=73,protein=12,fat=1.5,carbs=2.7,basis_g=100,aliases=[]))
        kind, food=ready_ingredient(data)
        self.assertEqual(kind,'ru')
        self.assertEqual((food.kcal,food.protein,food.fat,food.carbs),(73,12,1.5,2.7))

    def test_label_per_serving_converts_to_per_100g(self):
        kind, food=ready_ingredient(dict(name='Yoghurt',kcal=120,protein=20,
                        fat=0,carbs=10,basis_g=200))
        self.assertEqual(kind,'en')
        self.assertEqual((food.kcal,food.protein,food.fat,food.carbs),(60,10,0,5))

    def test_unknown_basis_refused(self):
        with self.assertRaises(ValueError):
            ready_ingredient(dict(name='Unknown',kcal=120,protein=20,fat=1,carbs=5))

    def test_zero_is_a_valid_explicit_value(self):
        kind,food=ready_ingredient(dict(name='Специи',basis_g=100,kcal=50,
                                       protein=0,fat=0,carbs=10))
        self.assertEqual(food.fat,0)

    def test_pending_ingredients_are_private_to_each_user_and_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=str(Path(tmp)/'db.sqlite3')
            db=FoodDB(path)
            db.save_pending_ingredient(101,{'name':'Skyr','kcal':60})
            self.assertIsNone(db.pending_ingredient(202))
            self.assertEqual(FoodDB(path).pending_ingredient(101)['name'],'Skyr')
            db.clear_pending_ingredient(101)
            self.assertIsNone(db.pending_ingredient(101))

    def test_restaurant_photo_estimated_macros_are_labeled(self):
        dish=item('Паста с соусом',300,estimate=True,
                  values=(180,7.3,6,25),weight_estimated=True)
        calculate_from_table(SimpleNamespace(items=[dish]),[],allow_estimates=True)
        self.assertEqual((dish.kcal,dish.protein,dish.fat,dish.carbs),(540,21.9,18,75))
        self.assertIn('ОЦЕНКА ИИ',dish.source_tab)

    def test_restaurant_estimate_not_allowed_for_normal_unspecified_dish(self):
        dish=item('Паста с соусом',300,estimate=True,values=(180,7.3,6,25))
        with self.assertRaises(MissingReferences):
            calculate_from_table(SimpleNamespace(items=[dish]),[],allow_estimates=False)

    def test_estimates_without_explanation_rejected(self):
        dish=item('Рагу',200,values=(180,7.3,6,25))
        with self.assertRaises(MissingReferences):
            calculate_from_table(SimpleNamespace(items=[dish]),[],allow_estimates=True)

    def test_missing_weight_still_rejected(self):
        dish=item('Столовский обед',None,estimate=True,values=(180,7.3,6,25))
        with self.assertRaises(MissingReferences):
            calculate_from_table(SimpleNamespace(items=[dish]),[],allow_estimates=True)

    def test_sheet_wins_even_for_photo_estimate(self):
        dish=item('Творог',200,estimate=True,values=(180,7.3,6,25),weight_estimated=True)
        refs=[Ingredient('Творог',(),73,12,1.5,2.7,source_tab='База ингредиентов')]
        calculate_from_table(SimpleNamespace(items=[dish]),refs,allow_estimates=True)
        self.assertEqual(dish.kcal,146)
        self.assertEqual(dish.source_tab,'База ингредиентов')
        self.assertTrue(dish.weight_estimated)

    def test_no_ai_guesses_for_simple_known_food(self):
        dish=item('Banana',100,estimate=True,values=(180,7,8,25))
        refs=[Ingredient('Banana',(),89,1.09,0.33,22.84,source_tab='Ingredients base')]
        calculate_from_table(SimpleNamespace(items=[dish]),refs,allow_estimates=True)
        self.assertEqual(dish.kcal,89)
        self.assertEqual(dish.protein,1.09)


if __name__=='__main__':
    unittest.main()
