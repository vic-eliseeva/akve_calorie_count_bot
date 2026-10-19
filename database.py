"""Small, persistent SQLite storage for one private two-person Telegram group."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any


class FoodDB:
    def __init__(self, db_path: str):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=20)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize(self):
        with self.connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS members (
                    telegram_user_id INTEGER PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    calories_goal REAL,
                    protein_goal REAL,
                    joined_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS meals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    telegram_user_id INTEGER NOT NULL,
                    day TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    title TEXT NOT NULL,
                    source_text TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    kcal REAL NOT NULL,
                    protein REAL NOT NULL,
                    fat REAL NOT NULL,
                    carbs REAL NOT NULL,
                    ingredients_json TEXT NOT NULL,
                    assumptions TEXT NOT NULL DEFAULT '',
                    confidence TEXT NOT NULL DEFAULT 'medium',
                    source_message_id INTEGER NOT NULL,
                    response_message_id INTEGER,
                    FOREIGN KEY(telegram_user_id) REFERENCES members(telegram_user_id),
                    UNIQUE(chat_id, telegram_user_id, source_message_id)
                );
                CREATE TABLE IF NOT EXISTS pending_ingredients (
                    telegram_user_id INTEGER PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_meal_day ON meals(chat_id, day, telegram_user_id);
                CREATE INDEX IF NOT EXISTS idx_meal_response ON meals(chat_id, response_message_id);
            """)

    def members(self, chat_id: int) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM members WHERE chat_id = ? ORDER BY joined_at", (chat_id,)).fetchall()
            return [dict(r) for r in rows]

    def get_member(self, user_id: int) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM members WHERE telegram_user_id = ?", (user_id,)).fetchone()
            return dict(row) if row else None

    def join(self, chat_id: int, user_id: int, name: str, now: str) -> tuple[bool, str]:
        with self.connect() as db:
            existing = db.execute("SELECT chat_id FROM members WHERE telegram_user_id = ?", (user_id,)).fetchone()
            if existing:
                if existing["chat_id"] == chat_id:
                    db.execute("UPDATE members SET name = ? WHERE telegram_user_id = ?", (name, user_id))
                    return True, "already"
                return False, "another_group"
            number = db.execute("SELECT COUNT(*) AS n FROM members WHERE chat_id = ?", (chat_id,)).fetchone()["n"]
            if number >= 2:
                return False, "full"
            db.execute(
                "INSERT INTO members (telegram_user_id, chat_id, name, joined_at) VALUES (?, ?, ?, ?)",
                (user_id, chat_id, name, now),
            )
            return True, "joined"

    def pending_ingredient(self, user_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT payload_json FROM pending_ingredients WHERE telegram_user_id=?", (user_id,)).fetchone()
            return json.loads(row["payload_json"]) if row else None

    def save_pending_ingredient(self, user_id: int, data: dict) -> None:
        with self.connect() as conn:
            conn.execute("""INSERT INTO pending_ingredients (telegram_user_id,payload_json,updated_at)
                            VALUES (?,?,?) ON CONFLICT(telegram_user_id)
                            DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at""",
                         (user_id, json.dumps(data, ensure_ascii=False), datetime.now().isoformat()))

    def clear_pending_ingredient(self, user_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM pending_ingredients WHERE telegram_user_id=?", (user_id,))

    def set_goals(self, user_id: int, calories: float, protein: float) -> None:
        with self.connect() as db:
            db.execute("UPDATE members SET calories_goal = ?, protein_goal = ? WHERE telegram_user_id = ?", (calories, protein, user_id))

    @staticmethod
    def totals(items: list[dict]) -> dict[str, float]:
        return {field: round(sum(max(0.0, float(item.get(field, 0))) for item in items), 1)
                for field in ("kcal", "protein", "fat", "carbs")}

    def add_meal(self, *, chat_id: int, user_id: int, day: str, now: str,
                 title: str, source_text: str, source_kind: str, items: list[dict],
                 assumptions: str, confidence: str, source_message_id: int) -> int:
        t = self.totals(items)
        with self.connect() as db:
            cur = db.execute("""
                INSERT INTO meals (chat_id, telegram_user_id, day, timestamp, title, source_text,
                   source_kind, kcal, protein, fat, carbs, ingredients_json,
                   assumptions, confidence, source_message_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (chat_id, user_id, day, now, title, source_text, source_kind,
                  t['kcal'], t['protein'], t['fat'], t['carbs'], json.dumps(items, ensure_ascii=False),
                  assumptions, confidence, source_message_id))
            return cur.lastrowid

    def set_response_message(self, meal_id: int, response_message_id: int) -> None:
        with self.connect() as db:
            db.execute("UPDATE meals SET response_message_id = ? WHERE id = ?", (response_message_id, meal_id))

    def by_id(self, chat_id: int, meal_id: int) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM meals WHERE chat_id = ? AND id = ?", (chat_id, meal_id)).fetchone()
            return dict(row) if row else None

    def by_reply(self, chat_id: int, response_message_id: int) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM meals WHERE chat_id = ? AND response_message_id = ?",
                             (chat_id, response_message_id)).fetchone()
            return dict(row) if row else None

    def last(self, chat_id: int, user_id: int) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM meals WHERE chat_id=? AND telegram_user_id=? ORDER BY id DESC LIMIT 1",
                             (chat_id, user_id)).fetchone()
            return dict(row) if row else None

    def update_meal(self, chat_id: int, user_id: int, meal_id: int,
                    title: str, source_text: str, items: list[dict], assumptions: str, confidence: str) -> bool:
        t = self.totals(items)
        with self.connect() as db:
            cur = db.execute("""UPDATE meals SET title=?, source_text=?, kcal=?, protein=?, fat=?,
                 carbs=?, ingredients_json=?, assumptions=?, confidence=?
                 WHERE chat_id=? AND telegram_user_id=? AND id=?""",
                (title, source_text, t['kcal'], t['protein'], t['fat'], t['carbs'],
                 json.dumps(items, ensure_ascii=False), assumptions, confidence, chat_id, user_id, meal_id))
            return cur.rowcount > 0

    def remove(self, chat_id: int, user_id: int, meal_id: int) -> bool:
        with self.connect() as db:
            cur = db.execute("DELETE FROM meals WHERE chat_id=? AND telegram_user_id=? AND id=?",
                             (chat_id, user_id, meal_id))
            return cur.rowcount > 0

    def daily(self, chat_id: int, day: str, user_id: int | None = None) -> list[dict]:
        sql = "SELECT * FROM meals WHERE chat_id=? AND day=?"
        args: list[Any] = [chat_id, day]
        if user_id is not None:
            sql += " AND telegram_user_id=?"
            args.append(user_id)
        sql += " ORDER BY timestamp, id"
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, args).fetchall()]

    def export_csv(self, chat_id: int) -> bytes:
        out = io.StringIO()
        fields = ["id", "date", "person", "meal", "calories", "protein_g", "fat_g", "carbs_g", "confidence", "assumptions"]
        writer = csv.DictWriter(out, fieldnames=fields)
        writer.writeheader()
        with self.connect() as db:
            rows = db.execute("""SELECT meals.*, members.name FROM meals
                  JOIN members ON members.telegram_user_id=meals.telegram_user_id
                  WHERE meals.chat_id=? ORDER BY timestamp, meals.id""", (chat_id,)).fetchall()
            for r in rows:
                writer.writerow(dict(id=r['id'], date=r['day'], person=r['name'], meal=r['title'],
                                     calories=r['kcal'], protein_g=r['protein'], fat_g=r['fat'],
                                     carbs_g=r['carbs'], confidence=r['confidence'], assumptions=r['assumptions']))
        return out.getvalue().encode('utf-8-sig')
