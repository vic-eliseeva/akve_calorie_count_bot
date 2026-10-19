"""Google Sheets as a shared ingredient source and a daily macro export target.

The service account only touches the two configured tabs. Other tabs are left alone.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

INGREDIENT_HEADERS = ["Ingredient", "Aliases", "kcal_per_100g", "protein_per_100g", "fat_per_100g", "carbs_per_100g", "Notes", "Updated_at", "Updated_by"]
DEFAULT_SPREADSHEET_ID = '1w6s6N5IpNHtuYUHBT6QO3PJ2ZDsLVysq4iHIQXLZg1A'  # Existing AKVE calorie count master (not a secret)
DAILY_HEADERS = ["Date", "Telegram_user_id", "Name", "kcal", "protein_g", "fat_g", "carbs_g", "Meals_count", "Last_synced"]


def normalize_name(name: str) -> str:
    """Preserves % and decimal fractions, normalizes Cyrillic/Latin whitespace/case."""
    value = unicodedata.normalize('NFKC', name).lower().replace('ё', 'е')
    value = value.replace(',', '.')
    return re.sub(r'\s+', ' ', value).strip()


def sheet_range(tab: str, cell_range: str = 'A:Z') -> str:
    return "'" + tab.replace("'", "''") + "'!" + cell_range


@dataclass(frozen=True)
class Ingredient:
    name: str
    aliases: tuple[str, ...]
    kcal: float
    protein: float
    fat: float
    carbs: float
    notes: str = ''
    updated_at: str = ''
    updated_by: str = ''

    @classmethod
    def from_row(cls, row: list) -> 'Ingredient':
        cells = [str(x) for x in row] + [''] * max(0, 9-len(row))
        if not cells[0].strip():
            raise ValueError('Ingredient has no name')
        return cls(
            name=cells[0].strip(), aliases=tuple(a.strip() for a in cells[1].split('|') if a.strip()),
            kcal=float(cells[2]), protein=float(cells[3]), fat=float(cells[4]), carbs=float(cells[5]),
            notes=cells[6], updated_at=cells[7], updated_by=cells[8],
        )

    def to_row(self) -> list:
        return [self.name, ' | '.join(self.aliases), self.kcal, self.protein, self.fat,
                self.carbs, self.notes, self.updated_at, self.updated_by]


def validate_ingredient(item: Ingredient) -> None:
    if not item.name.strip() or len(item.name) > 120:
        raise ValueError('Ingredient name must be between 1 and 120 characters.')
    for x, limit in [('kcal', 1000), ('protein', 100), ('fat', 100), ('carbs', 100)]:
        v = getattr(item, x)
        if not 0 <= v <= limit:
            raise ValueError(f'{x} must be between 0 and {limit} per 100 g.')
    if len(item.aliases) > 20:
        raise ValueError('Up to 20 aliases are supported.')


def build_lookup(ingredients: list[Ingredient]) -> dict[str, Ingredient]:
    lookup: dict[str, Ingredient] = {}
    for ing in ingredients:
        for key in (ing.name, *ing.aliases):
            norm = normalize_name(key)
            if norm in lookup and normalize_name(lookup[norm].name) != normalize_name(ing.name):
                raise ValueError(f'Ingredient alias collision: {key} is mapped to multiple foods.')
            lookup[norm] = ing
    return lookup


def apply_reference_macros(result, references: list[Ingredient]) -> None:
    """Override AI-provided per-portion estimates only for verified reference + grams.

    This is deterministic, leaving unknown or unweighed ingredients estimated by AI.
    """
    lookup = build_lookup(references)
    for item in result.items:
        key = normalize_name(item.reference or '')
        grams = item.portion_g
        if key and grams is not None and key in lookup and 0 < grams <= 100000:
            source = lookup[key]
            for nutrient in ('kcal', 'protein', 'fat', 'carbs'):
                setattr(item, nutrient, round(getattr(source, nutrient) * grams / 100.0, 2))


class SheetsError(RuntimeError):
    pass


class GoogleSheets:
    """Low-level Sheets API client. No user OAuth: sheet shared with service account.

    `transport` can be replaced by an in-memory fake in tests.
    """
    BASE = 'https://sheets.googleapis.com/v4/spreadsheets'

    def __init__(self, spreadsheet_id: str, *, ingredients_tab: str = 'Bot Ingredients',
                 daily_tab: str = 'Bot Daily Totals', credentials_json: str = '',
                 credentials_file: str = '', transport=None):
        if not re.fullmatch(r'[a-zA-Z0-9_-]{12,}', spreadsheet_id):
            raise ValueError('GOOGLE_SHEET_ID must be the spreadsheet ID from its URL.')
        self.id = spreadsheet_id
        self.ingredients_tab = ingredients_tab
        self.daily_tab = daily_tab
        if ingredients_tab == daily_tab:
            raise ValueError('Google Sheets tab names must be different.')
        if transport is not None:
            self.session = transport
        else:
            from google.auth.transport.requests import AuthorizedSession
            from google.oauth2 import service_account
            if credentials_json:
                info = json.loads(credentials_json)
                credentials = service_account.Credentials.from_service_account_info(
                    info, scopes=['https://www.googleapis.com/auth/spreadsheets'])
            elif credentials_file:
                credentials = service_account.Credentials.from_service_account_file(
                    credentials_file, scopes=['https://www.googleapis.com/auth/spreadsheets'])
            else:
                raise ValueError('Set GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_FILE.')
            self.session = AuthorizedSession(credentials)

    def _request(self, method: str, endpoint: str, *, params=None, data=None):
        from urllib.parse import quote
        url = f'{self.BASE}/{self.id}' + endpoint
        response = self.session.request(method, url, params=params, json=data, timeout=25)
        if not response.ok:
            # Google often sends a meaningful status, but do not echo auth credentials.
            raise SheetsError(f'Google Sheets API returned HTTP {response.status_code}: {response.text[:450]}')
        return response.json() if response.content else {}

    def tabs(self) -> list[str]:
        data = self._request('GET', '', params={'fields': 'sheets.properties.title'})
        return [s['properties']['title'] for s in data.get('sheets', [])]

    def values(self, tab: str) -> list[list]:
        from urllib.parse import quote
        range_ = quote(sheet_range(tab), safe='')
        data = self._request('GET', '/values/' + range_)
        return data.get('values', [])

    def put(self, tab: str, row: int, values: list) -> None:
        from urllib.parse import quote
        range_ = quote(sheet_range(tab, f'A{row}:I{row}'), safe='')
        self._request('PUT', '/values/' + range_, params={'valueInputOption': 'RAW'}, data={'values': [values]})

    def append(self, tab: str, values: list) -> None:
        from urllib.parse import quote
        range_ = quote(sheet_range(tab, 'A:I'), safe='')
        self._request('POST', '/values/' + range_ + ':append',
                      params={'valueInputOption': 'RAW', 'insertDataOption': 'INSERT_ROWS'},
                      data={'values': [values]})

    def ensure_tabs(self) -> None:
        existing = set(self.tabs())
        for tab, headers in [(self.ingredients_tab, INGREDIENT_HEADERS), (self.daily_tab, DAILY_HEADERS)]:
            if tab not in existing:
                self._request('POST', ':batchUpdate', data={'requests': [{'addSheet': {'properties': {'title': tab}}}]})
                self.put(tab, 1, headers)
            else:
                rows = self.values(tab)
                if not rows:
                    self.put(tab, 1, headers)
                elif rows[0][:len(headers)] != headers:
                    raise SheetsError(f'Tab "{tab}" exists with different column headers. No data was changed. Use a new tab name or adapt the mapping.')

    def ingredients(self) -> list[Ingredient]:
        rows = self.values(self.ingredients_tab)
        if rows and rows[0][:len(INGREDIENT_HEADERS)] != INGREDIENT_HEADERS:
            raise SheetsError(f'Unexpected headers on {self.ingredients_tab}; expected Bot Ingredients format.')
        foods = []
        for idx, row in enumerate(rows[1:], 2):
            if not row or not str(row[0]).strip():
                continue
            try:
                food = Ingredient.from_row(row)
                validate_ingredient(food)
                foods.append(food)
            except (ValueError, IndexError) as exc:
                raise SheetsError(f'Ingredient row {idx}: {exc}') from exc
        build_lookup(foods)
        return foods

    def upsert_ingredient(self, item: Ingredient) -> str:
        validate_ingredient(item)
        rows = self.values(self.ingredients_tab)
        ingredients = self.ingredients()
        original_keys = {normalize_name(i.name): i for i in ingredients}
        original = original_keys.get(normalize_name(item.name))
        combined = [i for i in ingredients if normalize_name(i.name) != normalize_name(item.name)] + [item]
        build_lookup(combined)  # Reject collisions before writing.
        if original:
            for row_num, row in enumerate(rows[1:], 2):
                if row and normalize_name(str(row[0])) == normalize_name(item.name):
                    self.put(self.ingredients_tab, row_num, item.to_row())
                    return 'updated'
        self.append(self.ingredients_tab, item.to_row())
        return 'added'

    def daily_rows(self) -> list[list]:
        rows = self.values(self.daily_tab)
        if rows and rows[0][:len(DAILY_HEADERS)] != DAILY_HEADERS:
            raise SheetsError('Daily totals sheet headers do not match expected format.')
        return rows

    def upsert_day(self, day: str, members: list[dict], db, chat_id: int, synced_at: str | None = None) -> int:
        """One row per user & day, idempotent across repeated syncs."""
        from datetime import date
        date.fromisoformat(day)
        rows = self.daily_rows()
        updated = 0
        instant = synced_at or datetime.now(timezone.utc).isoformat(timespec='seconds')
        for m in members:
            entries = db.daily(chat_id, day, m['telegram_user_id'])
            total = db.totals(entries)
            new_row = [day, str(m['telegram_user_id']), m['name'], total['kcal'], total['protein'],
                       total['fat'], total['carbs'], len(entries), instant]
            target_row = next((idx for idx, row in enumerate(rows[1:], 2) if len(row) >= 2
                               and str(row[0]) == day and str(row[1]) == str(m['telegram_user_id'])), None)
            if target_row is not None:
                self.put(self.daily_tab, target_row, new_row)
            else:
                self.append(self.daily_tab, new_row)
                rows.append(new_row)
            updated += 1
        return updated


def sheets_from_env() -> GoogleSheets | None:
    id_ = (os.getenv('GOOGLE_SHEET_ID') or DEFAULT_SPREADSHEET_ID).strip()
    if not id_:
        return None
    return GoogleSheets(id_, ingredients_tab=os.getenv('GOOGLE_INGREDIENTS_TAB', 'Bot Ingredients'),
                        daily_tab=os.getenv('GOOGLE_DAILY_TAB', 'Bot Daily Totals'),
                        credentials_json=os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON', '').strip(),
                        credentials_file=os.getenv('GOOGLE_SERVICE_ACCOUNT_FILE', '').strip())
