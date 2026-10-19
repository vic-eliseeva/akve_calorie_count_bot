import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database import FoodDB


class TestFoodDatabase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = FoodDB(str(Path(self.temp.name) / 'food.sqlite3'))
        self.chat = -123456
        self.now = '2026-10-16T18:00:00+02:00'
        self.assertEqual(self.db.join(self.chat, 1, 'Vika', self.now), (True, 'joined'))
        self.assertEqual(self.db.join(self.chat, 2, 'Tolya', self.now), (True, 'joined'))

    def tearDown(self):
        self.temp.cleanup()

    def test_only_two_members(self):
        self.assertEqual(self.db.join(self.chat, 3, 'Third', self.now), (False, 'full'))
        self.assertEqual(len(self.db.members(self.chat)), 2)

    def test_separate_macros_edit_and_delete(self):
        food = [{'name': 'eggs', 'kcal': 140, 'protein': 12, 'fat': 10, 'carbs': 1}]
        id1 = self.db.add_meal(chat_id=self.chat, user_id=1, day='2026-10-16', now=self.now,
                               title='Eggs', source_text='2 eggs', source_kind='text',
                               items=food, assumptions='', confidence='high', source_message_id=30)
        id2 = self.db.add_meal(chat_id=self.chat, user_id=2, day='2026-10-16', now=self.now,
                               title='Eggs', source_text='2 eggs', source_kind='voice',
                               items=food, assumptions='', confidence='high', source_message_id=31)
        self.db.set_response_message(id1, 42)
        self.assertEqual(self.db.by_reply(self.chat, 42)['id'], id1)
        self.assertEqual(self.db.daily(self.chat, '2026-10-16', 2)[0]['kcal'], 140)
        self.assertTrue(self.db.update_meal(self.chat, 1, id1, 'Three eggs', '3 eggs',
                        [{'name': 'eggs', 'kcal': 210, 'protein': 18, 'fat': 15, 'carbs': 1.5}],
                        '', 'high'))
        self.assertEqual(self.db.daily(self.chat, '2026-10-16', 1)[0]['kcal'], 210)
        self.assertEqual(self.db.daily(self.chat, '2026-10-16', 2)[0]['kcal'], 140)
        self.assertFalse(self.db.remove(self.chat, 2, id1))
        self.assertTrue(self.db.remove(self.chat, 1, id1))
        self.assertEqual(len(self.db.daily(self.chat, '2026-10-16', 1)), 0)

    def test_copy_export(self):
        self.db.set_goals(1, 2200, 150)
        self.assertEqual(self.db.get_member(1)['calories_goal'], 2200)
        self.db.add_meal(chat_id=self.chat, user_id=1, day='2026-10-16', now=self.now,
                         title='Rice', source_text='rice', source_kind='text',
                         items=[{'name': 'Rice 100g', 'kcal': 130, 'protein': 2, 'fat': 1, 'carbs': 28}],
                         assumptions='', confidence='medium', source_message_id=32)
        output = self.db.export_csv(self.chat).decode('utf-8-sig')
        self.assertIn('Vika', output)
        self.assertIn('Rice', output)
        self.assertIn('protein_g', output)

    def test_duplicate_message_rejected(self):
        kwargs = dict(chat_id=self.chat, user_id=1, day='2026-10-16', now=self.now,
                      title='Rice', source_text='rice', source_kind='text',
                      items=[{'name': 'Rice', 'kcal': 100, 'protein': 3, 'fat': 1, 'carbs': 22}],
                      assumptions='', confidence='medium', source_message_id=44)
        self.db.add_meal(**kwargs)
        with self.assertRaises(Exception):
            self.db.add_meal(**kwargs)


if __name__ == '__main__':
    unittest.main()
