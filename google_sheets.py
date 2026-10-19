"""Google Sheets as a shared ingredient source and a daily macro export target.

The service account only touches the two configured tabs. Other tabs are left alone.
"""
from __future__ import annotations

import json
import math
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SOURCE_TABS = ('База продуктов', 'База ингредиентов', 'Ingredients base')
TAB_FOR_KIND = {'custom':'База продуктов', 'ru':'База ингредиентов', 'en':'Ingredients base'}
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
    source_tab: str = ''


def validate_ingredient(item: Ingredient):
    if not item.name.strip() or len(item.name) > 200:
        raise ValueError('Ingredient name must have 1–200 characters.')
    for nutrient, upper in [('kcal', 1000), ('protein', 100), ('fat', 100), ('carbs', 100)]:
        v = getattr(item, nutrient)
        if not math.isfinite(v) or not 0 <= v <= upper:
            raise ValueError(f'{nutrient} must be finite and between 0 and {upper} per 100g.')


def build_lookup(items: list[Ingredient]) -> dict[str, Ingredient]:
    """First tab takes priority; aliases never override canonical names."""
    lookup = {}
    for food in items:
        lookup.setdefault(normalize_name(food.name), food)
    for food in items:
        for alias in food.aliases:
            lookup.setdefault(normalize_name(alias), food)
    return lookup


class MissingReferences(ValueError):
    def __init__(self, missing: list[str], missing_weights: list[str] | None = None):
        self.missing = list(dict.fromkeys(missing))
        self.missing_weights = list(dict.fromkeys(missing_weights or []))
        super().__init__('Unknown ingredient or portion. Nothing may be logged.')


def calculate_from_table(result, references: list[Ingredient], online_lookup=None,
                         allow_estimates: bool = False):
    """First spreadsheet; then validated USDA; lastly visibly labeled restaurant estimate.

    All meals reject incomplete items rather than silently omitting components.
    """
    lookup = build_lookup(references)
    missing, missing_weights = [], []
    for item in result.items:
        item.kcal = item.protein = item.fat = item.carbs = 0
        item.source_url = ''
        item.per100_kcal = item.per100_protein = item.per100_fat = item.per100_carbs = None
        item.source_tab = ''
        grams = item.portion_g
        if grams is None or not math.isfinite(grams) or grams <= 0 or grams > 100000:
            missing_weights.append(item.name)
            continue
        key = normalize_name(item.reference or '')
        if not key or key not in lookup:
            key = normalize_name(item.name)
        source = lookup.get(key)
        if source is None and online_lookup is not None:
            source = online_lookup(item)
            if source is not None:
                item.source_tab = 'USDA FoodData Central (среднее онлайн)'
                item.source_url = source.url
                item.reference = source.description
                item.name = source.name
        if source is None:
            estimated = [getattr(item, 'estimated_per100_'+nutrient, None)
                         for nutrient in ('kcal','protein','fat','carbs')]
            safe = (allow_estimates
                    and bool(str(getattr(item, 'estimate_note', '') or '').strip())
                    and all(isinstance(x, (int, float)) and math.isfinite(x) for x in estimated)
                    and 0 < estimated[0] <= 1000
                    and all(0 <= x <= 100 for x in estimated[1:]))
            if safe:
                item.reference = item.name
                item.source_tab = 'ОЦЕНКА ИИ по фото/описанию (не справочник)'
                item.source_url = ''
                for nutrient, value in zip(('kcal','protein','fat','carbs'), estimated):
                    setattr(item, 'per100_'+nutrient, value)
                    setattr(item, nutrient, round(value * grams / 100, 2))
                continue
            missing.append(item.name)
            continue
        if not item.source_url:
            item.reference = source.name
            item.name = source.name
            item.source_tab = source.source_tab
        for nutrient in ('kcal','protein','fat','carbs'):
            per100 = getattr(source, nutrient)
            setattr(item, 'per100_'+nutrient, per100)
            setattr(item, nutrient, round(per100 * grams / 100, 2))
    if missing or missing_weights or not result.items:
        if not result.items:
            missing.append('Не удалось распознать ингредиенты')
        raise MissingReferences(missing, missing_weights)

class SheetsError(RuntimeError):
    pass


class GoogleSheets:
    """Low-level Sheets API client. No user OAuth: sheet shared with service account.

    `transport` can be replaced by an in-memory fake in tests.
    """
    BASE = 'https://sheets.googleapis.com/v4/spreadsheets'

    def __init__(self, spreadsheet_id: str, *, daily_tab: str = 'Bot Daily Totals', credentials_json: str = '',
                 credentials_file: str = '', transport=None):
        if not re.fullmatch(r'[a-zA-Z0-9_-]{12,}', spreadsheet_id):
            raise ValueError('GOOGLE_SHEET_ID must be the spreadsheet ID from its URL.')
        self.id = spreadsheet_id
        self.daily_tab = daily_tab
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
        last_col = chr(ord('A') + len(values) - 1)
        range_ = quote(sheet_range(tab, f'A{row}:{last_col}{row}'), safe='')
        self._request('PUT', '/values/' + range_, params={'valueInputOption': 'RAW'}, data={'values': [values]})

    def append(self, tab: str, values: list) -> None:
        from urllib.parse import quote
        last_col = chr(ord('A') + len(values) - 1)
        range_ = quote(sheet_range(tab, 'A:' + last_col), safe='')
        self._request('POST', '/values/' + range_ + ':append',
                      params={'valueInputOption': 'RAW', 'insertDataOption': 'INSERT_ROWS'},
                      data={'values': [values]})

    def ensure_tabs(self) -> None:
        found = set(self.tabs())
        missing = [tab for tab in (*SOURCE_TABS, self.daily_tab) if tab not in found]
        if missing:
            raise SheetsError('Required Google Sheet tabs missing: ' + ', '.join(missing))
        for tab in SOURCE_TABS:
            rows = self.values(tab)
            if not rows or len(rows[0]) < 6:
                raise SheetsError('Ingredient tab has wrong structure: ' + tab)
        daily = self.values(self.daily_tab)
        if not daily or daily[0][:len(DAILY_HEADERS)] != DAILY_HEADERS:
            raise SheetsError('Daily summary tab has unexpected headers; nothing changed.')

    def ingredients(self) -> list[Ingredient]:
        if any(tab not in self.tabs() for tab in SOURCE_TABS):
            raise SheetsError('Missing one or more source ingredient tabs.')
        foods = []
        for tab in SOURCE_TABS:
            for row in self.values(tab)[1:]:
                if len(row) < 6 or row[1] is None or not str(row[1]).strip():
                    continue
                name = str(row[1]).strip()
                vals = row[2:6]
                aliases = []
                if tab == 'База ингредиентов' and len(row) >= 8 and not str(row[2] or '').strip():
                    vals = row[4:8]
                    if row[3] is not None and str(row[3]).strip():
                        aliases = [str(row[3]).strip()]
                try:
                    per100 = [float(str(x).replace(',', '.')) for x in vals]
                except (ValueError, TypeError):
                    continue
                if tab == 'База продуктов':
                    names = [x.strip() for x in name.split(',') if x.strip()]
                    if not names:
                        continue
                    name, aliases = names[0], names[1:]
                item = Ingredient(name, tuple(aliases), *per100,
                                  notes=str(row[0]) if row[0] is not None else '', source_tab=tab)
                try:
                    validate_ingredient(item)
                except ValueError:
                    continue
                foods.append(item)
        return foods

    def add_ingredient(self, kind: str, item: Ingredient) -> str:
        """Append provided values ONLY to the selected original tab."""
        tab = TAB_FOR_KIND.get(kind.lower())
        if tab is None:
            raise ValueError('Choose source: ru, en or custom.')
        validate_ingredient(item)
        existing = build_lookup(self.ingredients())
        for alias in (item.name, *item.aliases):
            if normalize_name(alias) in existing:
                raise ValueError(f'Already in Google Sheet: {alias}. Edit the source row directly to correct values.')
        if kind.lower() == 'custom':
            names = ', '.join([item.name, *item.aliases])
            values = ['', names, item.kcal, item.protein, item.fat, item.carbs, '', '']
        else:
            if item.aliases:
                raise ValueError('Aliases can only be added to custom products.')
            values = ['Added from Telegram', item.name, item.kcal, item.protein, item.fat, item.carbs]
        self.append(tab, values)
        return tab

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
    return GoogleSheets(id_, daily_tab=os.getenv('GOOGLE_DAILY_TAB', 'Bot Daily Totals'),
                        credentials_json=os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON', '').strip(),
                        credentials_file=os.getenv('GOOGLE_SERVICE_ACCOUNT_FILE', '').strip())
