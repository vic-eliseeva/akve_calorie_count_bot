"""Offline acceptance tests: spreadsheet priority, USDA lookups and rejected guesses."""
import unittest
from types import SimpleNamespace

from google_sheets import Ingredient, MissingReferences, calculate_from_table
from usda import USDAFoodLookup


class MockResponse:
    def __init__(self, foods):
        self.foods = foods
    def raise_for_status(self):
        pass
    def json(self):
        return {"foods": self.foods}


def food(identifier, description, kcal, protein, fat, carbs, dtype="SR Legacy"):
    return {
        "fdcId": identifier,
        "description": description,
        "dataType": dtype,
        "foodNutrients": [
            {"nutrientId": 1008, "nutrientNumber": "208", "value": kcal, "unitName": "KCAL"},
            {"nutrientId": 1003, "nutrientNumber": "203", "value": protein, "unitName": "G"},
            {"nutrientId": 1004, "nutrientNumber": "204", "value": fat, "unitName": "G"},
            {"nutrientId": 1005, "nutrientNumber": "205", "value": carbs, "unitName": "G"},
        ]
    }


class Session:
    def __init__(self):
        self.calls = []
    def get(self, url, params, timeout):
        self.calls.append((url, params))
        query = params["query"]
        foods = {
            "banana": [food(173944,"Bananas, raw",89,1.09,0.33,22.84),
                       food(999911,"Bananas, dried",346,3.8,1.8,88)],
            "mango": [food(169910,"Mangos, raw",60,0.82,0.38,14.98)],
            "lasagna": [food(111222,"Lasagna, with meat",150,7,8,11)],
            "apple": [food(171688,"Apples, raw, with skin",52,0.26,0.17,13.81)],
            "mystery": [],
        }
        return MockResponse(foods.get(query, []))


def mealitem(name, grams, *, query=None, simple=False, reference=None):
    return SimpleNamespace(name=name, reference=reference,
                           lookup_query=query, simple_ingredient=simple,
                           portion_g=grams, kcal=555, protein=444, fat=333, carbs=222,
                           source_tab="", source_url="",
                           per100_kcal=None, per100_protein=None,
                           per100_fat=None, per100_carbs=None)


class PublicLookupTests(unittest.TestCase):
    def setUp(self):
        self.session = Session()
        self.online = USDAFoodLookup("DUMMY_KEY", session=self.session)

    def test_banana_online_values_and_cited_source(self):
        banana=mealitem("banana",100,query="banana",simple=True)
        calculate_from_table(SimpleNamespace(items=[banana]), [], self.online.resolve)
        self.assertEqual((banana.kcal,banana.protein,banana.fat,banana.carbs),
                         (89,1.09,0.33,22.84))
        self.assertIn("USDA",banana.source_tab)
        self.assertIn("173944",banana.source_url)
        self.assertEqual(banana.reference,"Bananas, raw")

    def test_mango_uses_real_per_100g_arithmetic(self):
        mango=mealitem("mango",200,query="mango",simple=True)
        calculate_from_table(SimpleNamespace(items=[mango]), [], self.online.resolve)
        self.assertEqual((mango.kcal,mango.protein,mango.fat,mango.carbs),
                         (120,1.64,0.76,29.96))
        self.assertEqual(mango.per100_kcal,60)

    def test_spreadsheet_has_priority_no_web_call(self):
        item=mealitem("banana",120,query="banana",simple=True)
        sheet=[Ingredient("banana",(),90,1,0.5,22,source_tab="Ingredients base")]
        calculate_from_table(SimpleNamespace(items=[item]),sheet,self.online.resolve)
        self.assertEqual(item.kcal,108)
        self.assertEqual(item.source_tab,"Ingredients base")
        self.assertEqual(item.source_url,"")
        self.assertEqual(self.session.calls,[])

    def test_composite_lasagna_never_automatically_matched(self):
        item=mealitem("lasagna",250,query="lasagna",simple=False)
        with self.assertRaises(MissingReferences):
            calculate_from_table(SimpleNamespace(items=[item]),[],self.online.resolve)
        self.assertEqual(len(self.session.calls),0)

    def test_even_if_ai_claims_mix_simple_its_rejected(self):
        item=mealitem("lasagna",200,query="lasagna",simple=True)
        with self.assertRaises(MissingReferences):
            calculate_from_table(SimpleNamespace(items=[item]),[],self.online.resolve)

    def test_no_measured_grams_no_calculation_even_online(self):
        item=mealitem("mango",None,query="mango",simple=True)
        with self.assertRaises(MissingReferences) as ctx:
            calculate_from_table(SimpleNamespace(items=[item]),[],self.online.resolve)
        self.assertIn("mango",ctx.exception.missing_weights)
        self.assertEqual(self.session.calls,[])

    def test_unknown_no_fabricated_fallback(self):
        item=mealitem("mystery",100,query="mystery",simple=True)
        with self.assertRaises(MissingReferences):
            calculate_from_table(SimpleNamespace(items=[item]),[],self.online.resolve)
        self.assertEqual(item.kcal,0)

    def test_one_unknown_blocks_whole_meal(self):
        known=mealitem("banana",100,query="banana",simple=True)
        unknown=mealitem("mystery",60,query="mystery",simple=True)
        with self.assertRaises(MissingReferences):
            calculate_from_table(SimpleNamespace(items=[known,unknown]),[],self.online.resolve)

    def test_lookup_is_cached(self):
        self.assertIsNotNone(self.online.lookup("banana"))
        self.assertIsNotNone(self.online.lookup("banana"))
        self.assertEqual(len(self.session.calls),1)

    def test_state_mismatch_blocks_cooked_as_raw(self):
        self.assertIsNone(self.online.lookup("banana raw") if False else
                          USDAFoodLookup("DUMMY_KEY",session=type("CookedSession",(),{
                              "get":lambda _,url,params,timeout: MockResponse([
                                  food(234,"Bananas, cooked",120,1,1,25)])
                          })()).lookup("banana"))


if __name__ == '__main__':
    unittest.main()
