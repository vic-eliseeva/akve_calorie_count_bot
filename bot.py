"""Two people + one Telegram nutrition assistant, backed by local SQLite."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
from datetime import datetime, timedelta, date, time
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import BotCommand, InputFile, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from database import FoodDB
from nutrition import FoodResult, NutritionAI
from ingredient_chat import has_ingredient_intent, combine, ready_ingredient, prompt_for
from google_sheets import GoogleSheets, Ingredient, sheets_from_env, build_lookup, normalize_name, calculate_from_table, MissingReferences
from usda import USDAFoodLookup

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
logger = logging.getLogger('duo-food-bot')

TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
API_KEY = os.getenv('GEMINI_API_KEY', '').strip()
CHAT_ID_STR = os.getenv('ALLOWED_CHAT_ID', '').strip()
ALLOWED_CHAT_ID = int(CHAT_ID_STR) if CHAT_ID_STR else None
TIMEZONE = ZoneInfo(os.getenv('TIMEZONE', 'Europe/Copenhagen'))
DB = FoodDB(os.getenv('DB_PATH', './data/food.sqlite3'))
AI: NutritionAI | None = None
SHEETS: GoogleSheets | None = None
ONLINE_LOOKUP = USDAFoodLookup()

HELP_TEXT = """Дневник питания для двоих

Пиши обычным текстом или отправляй фото и голосовые сообщения:
• «Добавь мне 150 г манго и 100 г банана»
• «Вот мой обед» + фото — я примерно оценю порцию, укажу допущения
• «Я поела пасту в столовой» — оценка блюда с предупреждением
• «Сколько КБЖУ в 200 г риса?» — рассчитаю, но не запишу
• «Добавь в базу сыр X: на 100 г 250 ккал, Б 20, Ж 18, У 2»
  Можно уточнять недостающие значения следующими сообщениями.
Справочник → USDA → явно помеченная примерная оценка кафе/фото.
Всегда показываю расчёт по каждому ингредиенту и источники.

Команды необязательны для еды и добавления ингредиентов:
/join — подключить себя (не больше двух участников)
/today — сумма КБЖУ обоих за сегодня
/mine — мои блюда
/yesterday — вчерашние суммы
/goal 2200 150 — установить суточную цель
/editlast ... — исправить последнее блюдо
/undo — удалить последнюю запись
/copy ID 0.5 — скопировать половину блюда
/ingredient NAME — найти продукт в справочнике
/ingredientadd — вручную добавить продукт в базу
/sync — синхронизировать Google Sheet
/sheetstatus — состояние Google Sheets
/chatid — ID группы
/help — помощь
Для исправления можно ответить прямо на расчёт бота.
"""


def now_local() -> datetime:
    return datetime.now(TIMEZONE)


def is_group(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.type in ('group', 'supergroup'))


def permitted(update: Update) -> bool:
    return is_group(update) and ALLOWED_CHAT_ID is not None and update.effective_chat.id == ALLOWED_CHAT_ID


def owner(update: Update) -> dict | None:
    if not permitted(update) or not update.effective_user:
        return None
    m = DB.get_member(update.effective_user.id)
    return m if m and m['chat_id'] == ALLOWED_CHAT_ID else None


async def demand_owner(update: Update) -> bool:
    if not permitted(update):
        await update.effective_message.reply_text('Группа пока не настроена. Отправь /chatid, укажи ALLOWED_CHAT_ID в Railway и перезапусти бота.')
        return False
    if owner(update) is None:
        await update.effective_message.reply_text('Сначала отправь /join. В дневнике могут быть два человека.')
        return False
    return True


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(HELP_TEXT)


async def chatid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        await update.effective_message.reply_text('Создай группу с партнёром и ботом, затем отправь туда /chatid.')
        return
    await update.effective_message.reply_text(f'ID группы: {update.effective_chat.id}\nСкопируй этот ID в переменную ALLOWED_CHAT_ID в Railway. Сам токен бота никому не отправляй.')


async def join_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not permitted(update):
        await update.effective_message.reply_text('Сначала настрой ALLOWED_CHAT_ID. Отправь /chatid в вашу группу.')
        return
    u = update.effective_user
    name = u.first_name + (f' {u.last_name}' if u.last_name else '')
    ok, reason = DB.join(update.effective_chat.id, u.id, name, now_local().isoformat())
    if ok:
        reply = f'{name}: ' + ('уже подключён(а).' if reason == 'already' else 'подключён(а). Теперь можно писать про еду, отправлять голосовые и фото.')
    else:
        reply = 'К дневнику уже подключены два человека.' if reason == 'full' else 'Этот аккаунт уже привязан к другой группе.'
    await update.effective_message.reply_text(reply)


async def goal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await demand_owner(update):
        return
    if len(context.args) != 2:
        await update.effective_message.reply_text('Пример команды: /goal 2200 150 — цель по калориям и белкам.')
        return
    try:
        calories, protein = [float(s.replace(',', '.')) for s in context.args]
        if not (500 <= calories <= 10000 and 0 <= protein <= 500):
            raise ValueError('out of range')
    except ValueError:
        await update.effective_message.reply_text('Укажи разумные положительные числа, например /goal 2200 150.')
        return
    DB.set_goals(update.effective_user.id, calories, protein)
    await update.effective_message.reply_text(f'Твои цели: {calories:g} ккал и {protein:g} г белка в день. У партнёра отдельные цели.')


def fmt(n: float | int) -> str:
    v = float(n)
    return f'{v:.6f}'.rstrip('0').rstrip('.')


def macros(t: dict) -> str:
    return f"{fmt(t['kcal'])} ккал | Б {fmt(t['protein'])} г | Ж {fmt(t['fat'])} г | У {fmt(t['carbs'])} г"


def sum_meals(meals: list[dict]) -> dict:
    return {f: sum(m[f] for m in meals) for f in ['kcal', 'protein', 'fat', 'carbs']}


def day_report(chat_id: int, day: str, only_user: int | None = None, detailed: bool = False) -> str:
    members = DB.members(chat_id)
    if only_user is not None:
        members = [m for m in members if m['telegram_user_id'] == only_user]
    lines = [f'Дневник питания · {day}']
    for m in members:
        meals = DB.daily(chat_id, day, m['telegram_user_id'])
        t = sum_meals(meals)
        lines.append(f"\n{m['name']}: {macros(t)}")
        if m['calories_goal'] is not None:
            lines.append(f"Цели: {fmt(t['kcal'])}/{fmt(m['calories_goal'])} ккал; белки {fmt(t['protein'])}/{fmt(m['protein_goal'])} г")
        if detailed:
            lines.extend(f"  #{x['id']} {x['title']} — {fmt(x['kcal'])} ккал" for x in meals[-20:])
        if not meals:
            lines.append('  Пока нет записанных блюд.')
    return '\n'.join(lines)[:3900]


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await demand_owner(update):
        await update.effective_message.reply_text(day_report(ALLOWED_CHAT_ID, now_local().date().isoformat()))


async def yesterday_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await demand_owner(update):
        day = (now_local().date() - timedelta(days=1)).isoformat()
        await update.effective_message.reply_text(day_report(ALLOWED_CHAT_ID, day))


async def mine_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await demand_owner(update):
        await update.effective_message.reply_text(day_report(ALLOWED_CHAT_ID, now_local().date().isoformat(), update.effective_user.id, detailed=True))


def ingredients(result: FoodResult) -> list[dict]:
    return [item.model_dump() for item in result.items]


def breakdown(items: list[dict], title: str, *, heading: str = 'Расчёт') -> str:
    """Each value is auditable, including clearly identified visual estimates."""
    lines = [f'{heading} · {title}', 'КБЖУ по ингредиентам (на 100 г × вес / 100):']
    for i, item in enumerate(items, 1):
        grams = item['portion_g']
        tab = item.get('source_tab') or 'Нет источника'
        lines.append(f"\n{i}. {item.get('reference') or item['name']} — {fmt(grams)} г"
                     + (' (вес оценён)' if item.get('weight_estimated') else '')
                     + f"\n  Источник: {tab}")
        if item.get('source_url'):
            lines.append('  Данные из открытой базы: ' + item['source_url'])
        if item.get('estimate_note'):
            lines.append('  Предположение: ' + str(item['estimate_note'])[:340])
        for nutrient, label, units in [('kcal', 'Ккал', 'ккал'), ('protein', 'Белки', 'г'),
                                       ('fat', 'Жиры', 'г'), ('carbs', 'Углеводы', 'г')]:
            lines.append(f"  {label}: {fmt(item['per100_'+nutrient])} × "
                         f"{fmt(grams)}/100 = {fmt(item[nutrient])} {units}")
    lines.append(f"\nИТОГО: {macros(DB.totals(items))}")
    if any(item.get('weight_estimated') or str(item.get('source_tab', '')).startswith('ОЦЕНКА ИИ')
           for item in items):
        lines.append('Внимание: здесь есть примерные оценки по фото/описанию, а не измеренные данные. '
                     'Можно ответить на сообщение с исправлением веса или состава.')
    return '\n'.join(lines)


async def show_long_reply(update: Update, message: str):
    """Telegram limits message size; never silently truncate calculations."""
    chunks = []
    while message:
        if len(message) <= 3700:
            chunks.append(message)
            break
        pos = message.rfind('\n', 0, 3700)
        if pos < 100:
            pos = 3700
        chunks.append(message[:pos])
        message = message[pos:].lstrip('\n')
    first = None
    for chunk in chunks:
        sent = await update.effective_message.reply_text(chunk)
        if first is None:
            first = sent.message_id
    return first


def missing_answer(exc: MissingReferences) -> str:
    lines = ['Пока не записал блюдо: не хватает надёжных данных.']
    if exc.missing:
        lines.append('Не нашла в твоей таблице или USDA: ' + ', '.join(exc.missing))
        lines.append('Можешь написать обычным текстом, например: '
                     '«Добавь в базу творог 1,5%, на 100 г: 73 ккал, '
                     'белки 12, жиры 1,5, углеводы 2,7». '
                     'Я запишу продукт в соответствующий лист.')
    if exc.missing_weights:
        lines.append('Уточни вес (г): ' + ', '.join(exc.missing_weights))
    lines.append('Когда уточним данные, отправь описание блюда снова.')
    return '\n'.join(lines)


async def analyze(text: str, media: bytes | None = None, mime: str | None = None,
                  force_log: bool = False, correction: dict | None = None) -> FoodResult:
    if SHEETS is None:
        raise ValueError('Spreadsheet credentials missing. Cannot log nutrition without references.')
    references = await asyncio.to_thread(SHEETS.ingredients)
    photo = bool(media and mime and mime.startswith('image/'))
    result = await asyncio.to_thread(AI.interpret, text=text, media=media, mime=mime,
                                     force_log=force_log, correction=correction,
                                     references=references, photo=photo)
    if result.action != 'ignore':
        allow_estimates = photo or bool(result.restaurant_estimate)
        await asyncio.to_thread(calculate_from_table, result, references, ONLINE_LOOKUP.resolve, allow_estimates)
        if any(item.weight_estimated or item.source_tab.startswith('ОЦЕНКА ИИ') for item in result.items):
            result.confidence = 'low'
    return result

async def sync_day(day: str) -> int:
    if not SHEETS:
        raise ValueError('Google Sheet not connected: set GOOGLE_SHEET_ID and service-account credentials.')
    return await asyncio.to_thread(SHEETS.upsert_day, day, DB.members(ALLOWED_CHAT_ID), DB, ALLOWED_CHAT_ID)


async def sync_past(day: str):
    # When someone edits an already-closed day, update the record immediately.
    if SHEETS and day < now_local().date().isoformat():
        try:
            await sync_day(day)
        except Exception:
            logger.exception('Automatic resync of corrected date failed: %s', day)


async def nightly_sync(context: ContextTypes.DEFAULT_TYPE):
    if not SHEETS or ALLOWED_CHAT_ID is None:
        return
    yesterday = (now_local().date() - timedelta(days=1)).isoformat()
    try:
        updated = await sync_day(yesterday)
        logger.info('Nightly Google Sheets sync complete: %s, %d people', yesterday, updated)
    except Exception:
        logger.exception('Nightly sync failed for %s. Use /sync %s after restoring service.', yesterday, yesterday)


async def save_and_confirm(update: Update, result: FoodResult, text: str, source_kind: str):
    if not result.items:
        await update.effective_message.reply_text('Пока ничего не записал. Напиши, что ты ел(а), и примерно сколько.')
        return
    stamp = now_local()
    try:
        mid = DB.add_meal(chat_id=ALLOWED_CHAT_ID, user_id=update.effective_user.id,
                          day=stamp.date().isoformat(), now=stamp.isoformat(),
                          title=result.title or 'Блюдо', source_text=text,
                          source_kind=source_kind, items=ingredients(result),
                          assumptions=result.assumptions, confidence=result.confidence,
                          source_message_id=update.effective_message.message_id)
    except Exception as exc:
        if 'UNIQUE constraint failed' in str(exc):
            return
        raise
    message = breakdown(ingredients(result), result.title, heading=f'Записал #{mid}')
    if result.assumptions:
        message += '\nДопущения: ' + result.assumptions[:600]
    message += '\nМожешь ответить на это сообщение, чтобы исправить состав или вес.'
    reply_id = await show_long_reply(update, message)
    DB.set_response_message(mid, reply_id)

async def process_food(update: Update, *, text: str, media: bytes | None = None,
                       mime: str | None = None, force_log: bool = False,
                       source_kind: str = 'text'):
    if not await demand_owner(update):
        return
    try:
        result = await analyze(text, media=media, mime=mime, force_log=force_log)
        if result.action == 'ignore':
            if force_log:
                await update.effective_message.reply_text('Не удалось распознать блюдо. Опиши еду и примерный вес.')
            return
        if result.action == 'question':
            await show_long_reply(update, breakdown(ingredients(result), result.title or 'Еда', heading='Только расчёт') + '\nНе записано.' if result.items else 'Пожалуйста, уточни, что в блюде, и хотя бы примерный вес.')
            return
        await save_and_confirm(update, result, text, source_kind)
    except MissingReferences as exc:
        if len(exc.missing) == 1 and exc.missing[0] != 'Не удалось распознать ингредиенты':
            DB.save_pending_ingredient(update.effective_user.id, {'name': exc.missing[0]})
        await show_long_reply(update, missing_answer(exc))
    except Exception:
        logger.exception('Nutrition analysis failed')
        await update.effective_message.reply_text('Не удалось обработать еду. Проверь Gemini и логи Railway, попробуй ещё раз.')


async def log_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = ' '.join(context.args).strip()
    if not text:
        await update.effective_message.reply_text('Например: /log 200 г риса, 150 г курицы, 10 г масла. Но можно написать просто сообщением.')
        return
    await process_food(update, text=text, force_log=True)


async def estimate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await demand_owner(update):
        return
    text = ' '.join(context.args).strip()
    if not text:
        await update.effective_message.reply_text('Например: /estimate 200 г курицы и 100 г риса.')
        return
    try:
        result = await analyze('Calculate without logging: ' + text)
        if result.items:
            message = breakdown(ingredients(result), result.title or 'Recipe', heading='Только расчёт')
        else:
            message = 'Не получилось определить состав или вес. Уточни, пожалуйста.'
        await show_long_reply(update, message + '\nНе записано.')
    except MissingReferences as exc:
        await show_long_reply(update, missing_answer(exc))
    except Exception:
        logger.exception('Calculation failed')
        await update.effective_message.reply_text('Не получилось рассчитать. Проверь Google Sheets и Gemini.')

async def revise_meal(update: Update, meal: dict | None, correction: str):
    if not await demand_owner(update):
        return
    if not meal:
        await update.effective_message.reply_text('Не нашла блюдо. Посмотри номер в /mine.')
        return
    if meal['telegram_user_id'] != update.effective_user.id:
        await update.effective_message.reply_text('Исправлять запись может только тот, кто её добавил.')
        return
    if not correction:
        await update.effective_message.reply_text('Напиши, что изменилось, например «в рисе было 150 г, а не 250».')
        return
    try:
        result = await analyze(correction, correction=meal, force_log=True)
        if not result.items:
            await update.effective_message.reply_text('Не поняла исправление. Уточни продукт и вес.')
            return
        DB.update_meal(ALLOWED_CHAT_ID, update.effective_user.id, meal['id'],
                       result.title or meal['title'], meal['source_text'] + '\nCorrection: ' + correction,
                       ingredients(result), result.assumptions, result.confidence)
        detail = breakdown(ingredients(result), result.title or meal['title'], heading=f"Исправил #{meal['id']}")
        sent_id = await show_long_reply(update, detail)
        DB.set_response_message(meal['id'], sent_id)
        await sync_past(meal['day'])
    except MissingReferences as exc:
        await show_long_reply(update, missing_answer(exc))
    except Exception:
        logger.exception('Correction failed')
        await update.effective_message.reply_text('Не удалось исправить блюдо. Проверь Google Sheets и Gemini.')

async def editlast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await demand_owner(update):
        meal = DB.last(ALLOWED_CHAT_ID, update.effective_user.id)
        await revise_meal(update, meal, ' '.join(context.args).strip())


async def edit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await demand_owner(update):
        return
    if len(context.args) < 2 or not context.args[0].isdigit():
        await update.effective_message.reply_text('Пример: /edit 12 масла было 5 г, а не 10 г')
        return
    await revise_meal(update, DB.by_id(ALLOWED_CHAT_ID, int(context.args[0])), ' '.join(context.args[1:]))


async def undo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await demand_owner(update):
        return
    last = DB.last(ALLOWED_CHAT_ID, update.effective_user.id)
    if not last:
        await update.effective_message.reply_text('Нет записи, которую можно удалить.')
        return
    DB.remove(ALLOWED_CHAT_ID, update.effective_user.id, last['id'])
    await sync_past(last['day'])
    await update.effective_message.reply_text(f"Deleted your latest entry #{last['id']} ({last['title']}).")


async def copy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await demand_owner(update):
        return
    try:
        if len(context.args) not in (1, 2):
            raise ValueError()
        mid = int(context.args[0])
        factor = float(context.args[1].replace(',', '.')) if len(context.args) == 2 else 1.0
        if not (0 < factor <= 10):
            raise ValueError()
    except (ValueError, IndexError):
        await update.effective_message.reply_text('Например /copy 12 0.75 — скопировать 75% блюда №12.')
        return
    source = DB.by_id(ALLOWED_CHAT_ID, mid)
    if not source:
        await update.effective_message.reply_text('Не нашла блюдо. Его номер можно посмотреть в /mine.')
        return
    items = json.loads(source['ingredients_json'])
    nutrients = ('kcal','protein','fat','carbs')
    if any(not all(item.get(k) is not None for k in
                   ('reference','portion_g',*(f'per100_{n}' for n in nutrients)))
           for item in items):
        await update.effective_message.reply_text('Старая запись не содержит исходных значений ингредиентов. Укажи порцию заново.')
        return
    for item in items:
        item['portion_g'] = round(item['portion_g'] * factor, 3)
        for n in nutrients:
            item[n] = round(item[f'per100_{n}'] * item['portion_g'] / 100, 2)
    try:
        new_id = DB.add_meal(chat_id=ALLOWED_CHAT_ID, user_id=update.effective_user.id,
                             day=now_local().date().isoformat(), now=now_local().isoformat(),
                             title=f"{source['title']} ×{factor:g}", source_text=f'Копия #{mid} ×{factor:g}',
                             source_kind='copy', items=items,
                             assumptions=f'Копия записи #{mid}; те же справочные значения.',
                             confidence=source['confidence'], source_message_id=update.effective_message.message_id)
        reply_id = await show_long_reply(update,
              breakdown(items, f"{source['title']} ×{factor:g}", heading=f'Записал #{new_id}'))
        DB.set_response_message(new_id, reply_id)
    except Exception:
        logger.exception('Copy failed')
        await update.effective_message.reply_text('Не удалось скопировать блюдо. Проверь логи Railway.')

async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await demand_owner(update):
        return
    data = DB.export_csv(ALLOWED_CHAT_ID)
    await update.effective_message.reply_document(
        InputFile(io.BytesIO(data), filename='duo_food_diary.csv'),
        caption='Both users, separated by person. Google Sheets: File > Import > Upload.')



async def require_sheet(update: Update) -> bool:
    if not await demand_owner(update):
        return False
    if not SHEETS:
        await update.effective_message.reply_text('Google Sheets пока не подключён. Проверь настройки доступа в Railway.')
        return False
    return True


async def handle_ingredient_chat(update: Update, text: str, *, force: bool = False) -> bool:
    """Handle explicit free-form additions or a user's pending clarification."""
    if not await demand_owner(update):
        return True
    user_id = update.effective_user.id
    pending = DB.pending_ingredient(user_id)
    if not (force or pending or has_ingredient_intent(text)):
        return False
    if text.lower().strip() in ('отмена', 'cancel', '/cancel', 'стоп'):
        DB.clear_pending_ingredient(user_id)
        await update.effective_message.reply_text('Хорошо, добавление продукта отменено.')
        return True
    try:
        change = await asyncio.to_thread(AI.parse_ingredient_change, text, pending)
        if change.action == 'ignore' and not force:
            return False  # A regular meal sent while an addition is pending.
        if change.action == 'ignore' and force:
            await update.effective_message.reply_text(
                'Напиши название и КБЖУ обычным текстом. Например: '
                '«Добавь в базу творог 1,5%, на 100 г: 73 ккал, '
                'белки 12, жиры 1,5, углеводы 2,7».')
            return True
        data = combine(pending, change)
        try:
            kind, item = ready_ingredient(data)
        except ValueError as exc:
            if str(exc).startswith('Не хватает:'):
                DB.save_pending_ingredient(user_id, data)
                await update.effective_message.reply_text(prompt_for(data))
                return True
            raise
        if not SHEETS:
            await update.effective_message.reply_text('Сначала подключи Google Sheets в Railway.')
            return True
        tab = await asyncio.to_thread(SHEETS.add_ingredient, kind, item)
        DB.clear_pending_ingredient(user_id)
        await update.effective_message.reply_text(
            f'Готово, добавил «{item.name}» в лист «{tab}».\n'
            f'На 100 г: {fmt(item.kcal)} ккал · Б {fmt(item.protein)} г · '
            f'Ж {fmt(item.fat)} г · У {fmt(item.carbs)} г.\n'
            'Можешь отправить блюдо ещё раз — теперь я использую эти значения.')
        return True
    except Exception as exc:
        logger.exception('Conversational ingredient addition failed')
        await update.effective_message.reply_text(
            'Не удалось добавить продукт: ' + str(exc)[:280] +
            '\nПроверь данные или напиши «отмена», чтобы начать заново.')
        return True


async def ingredientadd_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await demand_owner(update):
        return
    raw = (update.effective_message.text or '').partition(' ')[2].strip()
    # Preserve the old structured command for people who already use it.
    parts = [p.strip() for p in raw.split('|')]
    if len(parts) == 6 and parts[0].lower() in ('ru', 'en', 'custom'):
        try:
            kind = parts[0].lower()
            numbers = [float(x.replace(',', '.')) for x in parts[2:]]
            names = [x.strip() for x in parts[1].split(',') if x.strip()] if kind == 'custom' else [parts[1]]
            item = Ingredient(names[0], tuple(names[1:]), *numbers)
            tab = await asyncio.to_thread(SHEETS.add_ingredient, kind, item)
            DB.clear_pending_ingredient(update.effective_user.id)
            await update.effective_message.reply_text(f'Добавила «{item.name}» в лист «{tab}».')
            return
        except Exception as exc:
            await update.effective_message.reply_text('Не смогла сохранить: ' + str(exc)[:300])
            return
    await handle_ingredient_chat(update, raw, force=True)


async def ingredient_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_sheet(update):
        return
    query = ' '.join(context.args).strip()
    if not query:
        await update.effective_message.reply_text('Пример: /ingredient творог 1.5%')
        return
    try:
        found = build_lookup(await asyncio.to_thread(SHEETS.ingredients)).get(normalize_name(query))
        if not found:
            await update.effective_message.reply_text(f'Не нашла продукт: "{query}". Напиши «добавь в базу…», чтобы внести новый продукт.')
            return
        await update.effective_message.reply_text(
            f'{found.name} (на 100 г): {found.kcal:g} ккал | Б {found.protein:g} г | '
            f'Ж {found.fat:g} г | У {found.carbs:g} г.\nДругие названия: {", ".join(found.aliases) or "нет"}')
    except Exception:
        logger.exception('Ingredient lookup failed')
        await update.effective_message.reply_text('Пока не удаётся прочитать Google Sheets. Проверь /sheetstatus.')


async def ingredients_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_sheet(update):
        return
    try:
        foods = await asyncio.to_thread(SHEETS.ingredients)
        lines = [f'Ингредиентов в Google Sheet ({len(foods)}):']
        lines.extend(f'- {x.name} ({x.kcal:g} ккал/100 г)' for x in foods[:70])
        if len(foods) > 70:
            lines.append(f'…и ещё {len(foods)-70}. Найди нужный командой /ingredient NAME.')
        await update.effective_message.reply_text('\n'.join(lines)[:3900])
    except Exception:
        logger.exception('Ingredients list failed')
        await update.effective_message.reply_text('Не удалось прочитать Google Sheets. Проверь /sheetstatus.')


async def sync_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_sheet(update):
        return
    try:
        if len(context.args) > 1:
            raise ValueError('Напиши /sync или /sync YYYY-MM-DD')
        if context.args:
            date.fromisoformat(context.args[0])
            days = [context.args[0]]
        else:
            today = now_local().date()
            days = [(today - timedelta(days=1)).isoformat(), today.isoformat()]
        total = sum([await sync_day(day) for day in days])
        await update.effective_message.reply_text(f'Google Sheet обновлён: {", ".join(days)} ({total} строк по участникам). Повторный запуск обновит строки без дубликатов.')
    except Exception as exc:
        logger.exception('Manual Google Sheets sync failed')
        await update.effective_message.reply_text(f'Не удалось синхронизировать: {str(exc)[:450]}')


async def sheetstatus_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_sheet(update):
        return
    try:
        items = await asyncio.to_thread(SHEETS.ingredients)
        await update.effective_message.reply_text(
            f'Google Sheet подключён. Продуктов: {len(items)} ингредиентов.\n'
            f'Листы продуктов: База продуктов, База ингредиентов, Ingredients base\nЛист ежедневных итогов: {SHEETS.daily_tab}\n'
            'Автоматический экспорт: 00:05 по Копенгагену (за вчерашний день).')
    except Exception as exc:
        await update.effective_message.reply_text(f'Нет подключения к Google Sheet: {str(exc)[:450]}')


async def normal_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not permitted(update):
        return
    message = update.effective_message
    if not message or not owner(update):
        return
    text = (message.text or message.caption or '').strip()
    # In a shared chat, replying to a bot confirmation with text revises that specific meal.
    if text and not (message.photo or message.voice or message.audio):
        handled = await handle_ingredient_chat(update, text)
        if handled:
            return
    if message.reply_to_message and message.reply_to_message.from_user and message.reply_to_message.from_user.is_bot:
        existing = DB.by_reply(ALLOWED_CHAT_ID, message.reply_to_message.message_id)
        if existing and text and not (message.photo or message.voice):
            await revise_meal(update, existing, text)
            return
    media = None
    mime = None
    source_kind = 'text'
    try:
        if message.voice:
            source_kind = 'voice'
            if message.voice.file_size and message.voice.file_size > 12_000_000:
                await message.reply_text('Голосовое сообщение слишком большое (больше 12 МБ).')
                return
            telegram_file = await message.voice.get_file()
            mime = 'audio/ogg'  # Telegram voice notes are normally OGG/Opus.
        elif message.photo:
            source_kind = 'photo'
            telegram_file = await message.photo[-1].get_file()
            mime = 'image/jpeg'
        elif message.audio:
            source_kind = 'audio'
            telegram_file = await message.audio.get_file()
            mime = message.audio.mime_type or 'audio/mpeg'
        else:
            telegram_file = None
        if telegram_file is not None:
            output = io.BytesIO()
            await telegram_file.download_to_memory(out=output)
            media = output.getvalue()
            if len(media) > 12_000_000:
                await message.reply_text('Файл слишком большой (больше 12 МБ).')
                return
    except Exception:
        logger.exception('Failed to download Telegram media')
        await message.reply_text('Не удалось загрузить файл из Telegram. Попробуй ещё раз.')
        return
    if not text and media is None:
        return
    await process_food(update, text=text, media=media, mime=mime, source_kind=source_kind)


async def set_commands(application: Application):
    await application.bot.set_my_commands([
        BotCommand('join', 'Register one of the two people'),
        BotCommand('today', 'Show both daily totals'),
        BotCommand('mine', 'Show your meals today'),
        BotCommand('goal', 'Set your calorie and protein targets'),
        BotCommand('editlast', 'Correct your latest meal'),
        BotCommand('undo', 'Remove latest meal'),
        BotCommand('copy', 'Copy a meal to your diary'),
        BotCommand('estimate', 'Calculate using Sheet and USDA verified averages'),
        BotCommand('export', 'Export shared diary to CSV'),
        BotCommand('ingredientadd', 'Add per-100g values into ru/en/custom tab'),
        BotCommand('ingredient', 'Find reference ingredient in Russian or English'),
        BotCommand('ingredients', 'List reference ingredients'),
        BotCommand('sync', 'Sync both users daily macros to Google Sheets'),
        BotCommand('sheetstatus', 'Check Google Sheets connection'),
        BotCommand('help', 'All commands'),
    ])
    if SHEETS and ALLOWED_CHAT_ID is not None and DB.members(ALLOWED_CHAT_ID):
        # Catch up if the service restarted overnight, without duplicating previous rows.
        yesterday = (now_local().date() - timedelta(days=1)).isoformat()
        try:
            await sync_day(yesterday)
        except Exception:
            logger.exception('Startup catch-up export failed for %s. Use /sync when Sheets is restored.', yesterday)


def main():
    global AI, SHEETS
    if not TOKEN or TOKEN == 'put_your_botfather_token_here':
        raise SystemExit('Set TELEGRAM_BOT_TOKEN in .env or environment variables.')
    if not API_KEY or API_KEY == 'put_your_gemini_api_key_here':
        raise SystemExit('Set GEMINI_API_KEY in .env or environment variables.')
    AI = NutritionAI(API_KEY, os.getenv('GEMINI_MODEL', 'gemini-3.5-flash-lite'))
    SHEETS = sheets_from_env()
    if SHEETS:
        SHEETS.ensure_tabs()  # On error fail fast instead of silently missing exports.
    app = Application.builder().token(TOKEN).post_init(set_commands).build()
    for name, handler in [
        ('start', help_cmd), ('help', help_cmd), ('chatid', chatid_cmd),
        ('join', join_cmd), ('goal', goal_cmd), ('today', today_cmd),
        ('yesterday', yesterday_cmd), ('mine', mine_cmd),
        ('log', log_cmd), ('estimate', estimate_cmd),
        ('editlast', editlast_cmd), ('edit', edit_cmd),
        ('undo', undo_cmd), ('copy', copy_cmd), ('export', export_cmd),
        ('ingredientadd', ingredientadd_cmd), ('ingredient', ingredient_cmd),
        ('ingredients', ingredients_cmd), ('sync', sync_cmd), ('sheetstatus', sheetstatus_cmd),
    ]:
        app.add_handler(CommandHandler(name, handler))
    app.add_handler(MessageHandler(
        (filters.TEXT & ~filters.COMMAND) | filters.VOICE | filters.PHOTO | filters.AUDIO,
        normal_message,
    ))
    if SHEETS:
        if not app.job_queue:
            raise SystemExit('Install python-telegram-bot[job-queue] to enable nightly sheet synchronization.')
        app.job_queue.run_daily(nightly_sync, time=time(hour=0, minute=5, tzinfo=TIMEZONE), name='daily_google_sheet_export')
    logger.info('Starting Telegram bot. Allowed chat: %s. Database: %s; Sheet: %s', ALLOWED_CHAT_ID, DB.path, bool(SHEETS))
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == '__main__':
    main()
