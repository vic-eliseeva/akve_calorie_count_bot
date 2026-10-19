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
from google_sheets import GoogleSheets, Ingredient, sheets_from_env, build_lookup, normalize_name

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

HELP_TEXT = """Two-person food diary / Дневник питания на двоих

1. Each person sends /join once in this group.
2. Send a food description, photo, or voice note to log what you ate.
3. Each person's entries and daily totals stay separate.

Commands:
/join — register your Telegram account (max. 2)
/today — both people's totals today
/mine — your meals and macros today
/yesterday — both people's totals yesterday
/goal 2200 150 — set YOUR calories and protein targets
/estimate chicken 200g... — calculate without logging
/log chicken 200g... — force a food log
/editlast rice was 250g cooked — correct your most recent entry
/edit 12 oil was 5g — correct your entry #12
/undo — delete your latest entry
/copy 12 0.75 — log 75% of any meal #12 as YOUR portion
/export — CSV of both diaries, compatible with Google Sheets
/ingredientadd name | kcal | protein | fat | carbs | aliases — save macros per 100g to Google Sheet
/ingredient name — look up a stored ingredient or alias (Russian/English)
/ingredients — list stored ingredients
/sync — update current and yesterday's totals in the Google Sheet
/sync YYYY-MM-DD — resync a specific date
/sheetstatus — check your connected Google Sheet
/chatid — show the ID of this group, for initial setup
/help — this message

Tip: reply directly to the bot's meal confirmation with a correction.
Russian & English supported. Food photo/quantity estimates can be inaccurate.
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
        await update.effective_message.reply_text('This group is not configured. Send /chatid, set ALLOWED_CHAT_ID on the server, and restart.')
        return False
    if owner(update) is None:
        await update.effective_message.reply_text('Please register first with /join. Only two people can join.')
        return False
    return True


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(HELP_TEXT)


async def chatid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        await update.effective_message.reply_text('Create a Telegram group with your partner and this bot, then send /chatid in that group.')
        return
    await update.effective_message.reply_text(f'Group ID: {update.effective_chat.id}\nCopy it into ALLOWED_CHAT_ID in your bot settings. Do not share your bot token.')


async def join_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not permitted(update):
        await update.effective_message.reply_text('First configure ALLOWED_CHAT_ID. Send /chatid in your group.')
        return
    u = update.effective_user
    name = u.first_name + (f' {u.last_name}' if u.last_name else '')
    ok, reason = DB.join(update.effective_chat.id, u.id, name, now_local().isoformat())
    if ok:
        reply = f'{name}: ' + ('already registered.' if reason == 'already' else 'registered. Now send food text, photos or voice notes.')
    else:
        reply = 'The diary already has two people.' if reason == 'full' else 'This Telegram user belongs to a different diary.'
    await update.effective_message.reply_text(reply)


async def goal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await demand_owner(update):
        return
    if len(context.args) != 2:
        await update.effective_message.reply_text('Usage: /goal 2200 150  (kcal and protein grams)')
        return
    try:
        calories, protein = [float(s.replace(',', '.')) for s in context.args]
        if not (500 <= calories <= 10000 and 0 <= protein <= 500):
            raise ValueError('out of range')
    except ValueError:
        await update.effective_message.reply_text('Use sensible positive numbers, e.g. /goal 2200 150')
        return
    DB.set_goals(update.effective_user.id, calories, protein)
    await update.effective_message.reply_text(f'Your targets: {calories:g} kcal and {protein:g} g protein daily. Your partner has independent targets.')


def fmt(n: float | int) -> str:
    v = float(n)
    return str(int(round(v))) if abs(round(v) - v) < .05 else f'{v:.1f}'


def macros(t: dict) -> str:
    return f"{fmt(t['kcal'])} kcal | P {fmt(t['protein'])}g | F {fmt(t['fat'])}g | C {fmt(t['carbs'])}g"


def sum_meals(meals: list[dict]) -> dict:
    return {f: sum(m[f] for m in meals) for f in ['kcal', 'protein', 'fat', 'carbs']}


def day_report(chat_id: int, day: str, only_user: int | None = None, detailed: bool = False) -> str:
    members = DB.members(chat_id)
    if only_user is not None:
        members = [m for m in members if m['telegram_user_id'] == only_user]
    lines = [f'Food diary · {day}']
    for m in members:
        meals = DB.daily(chat_id, day, m['telegram_user_id'])
        t = sum_meals(meals)
        lines.append(f"\n{m['name']}: {macros(t)}")
        if m['calories_goal'] is not None:
            lines.append(f"Targets: {fmt(t['kcal'])}/{fmt(m['calories_goal'])} kcal; protein {fmt(t['protein'])}/{fmt(m['protein_goal'])}g")
        if detailed:
            lines.extend(f"  #{x['id']} {x['title']} — {fmt(x['kcal'])} kcal" for x in meals[-20:])
        if not meals:
            lines.append('  No meals logged.')
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


async def analyze(text: str, media: bytes | None = None, mime: str | None = None,
                  force_log: bool = False, correction: dict | None = None) -> FoodResult:
    # Google Sheet is authoritative. Never silently fall back to AI guesses if it cannot be read.
    references = await asyncio.to_thread(SHEETS.ingredients) if SHEETS else []
    return await asyncio.to_thread(AI.interpret, text=text, media=media, mime=mime,
                                   force_log=force_log, correction=correction,
                                   references=references)


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
        await update.effective_message.reply_text('I could not identify an edible portion. Please specify what and how much you ate.')
        return
    stamp = now_local()
    try:
        mid = DB.add_meal(chat_id=ALLOWED_CHAT_ID, user_id=update.effective_user.id,
                          day=stamp.date().isoformat(), now=stamp.isoformat(),
                          title=result.title or 'Meal', source_text=text,
                          source_kind=source_kind, items=ingredients(result),
                          assumptions=result.assumptions, confidence=result.confidence,
                          source_message_id=update.effective_message.message_id)
    except Exception as exc:
        if 'UNIQUE constraint failed' in str(exc):
            return
        raise
    total = DB.totals(ingredients(result))
    response = f"Saved #{mid} · {result.title}\n{macros(total)}"
    if result.assumptions:
        response += f"\nAssumptions: {result.assumptions[:450]}"
    if result.confidence == 'low':
        response += '\nLow-confidence estimate: exact weights would help.'
    response += '\nReply to this message to correct the meal.'
    sent = await update.effective_message.reply_text(response[:3900])
    DB.set_response_message(mid, sent.message_id)


async def process_food(update: Update, *, text: str, media: bytes | None = None,
                       mime: str | None = None, force_log: bool = False,
                       source_kind: str = 'text'):
    if not await demand_owner(update):
        return
    try:
        result = await analyze(text, media=media, mime=mime, force_log=force_log)
        if result.action == 'ignore':
            if force_log:
                await update.effective_message.reply_text('I could not recognize a meal. Describe the food and quantities.')
            return
        if result.action == 'question':
            await update.effective_message.reply_text((result.answer or 'Please specify what you ate.')[:3800])
            return
        await save_and_confirm(update, result, text, source_kind)
    except Exception:
        logger.exception('Nutrition analysis failed')
        await update.effective_message.reply_text('Food analysis failed. Check the Gemini API key, quota and logs; try again.')


async def log_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = ' '.join(context.args).strip()
    if not text:
        await update.effective_message.reply_text('Usage: /log 200g cooked rice, 150g chicken, 10g oil')
        return
    await process_food(update, text=text, force_log=True)


async def estimate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await demand_owner(update):
        return
    text = ' '.join(context.args).strip()
    if not text:
        await update.effective_message.reply_text('Usage: /estimate 200g chicken, 100g rice')
        return
    try:
        result = await analyze('Calculate nutrition for this food without logging: ' + text)
        if result.items:
            message = f"Estimate · {result.title}\n{macros(DB.totals(ingredients(result)))}"
            if result.assumptions:
                message += f'\nAssumptions: {result.assumptions}'
        else:
            message = result.answer or 'Not enough information to estimate.'
        await update.effective_message.reply_text(message[:3900] + '\nNot saved.')
    except Exception:
        logger.exception('Estimate failed')
        await update.effective_message.reply_text('Could not estimate. Check Gemini settings and try again.')


async def revise_meal(update: Update, meal: dict | None, correction: str):
    if not await demand_owner(update):
        return
    if not meal:
        await update.effective_message.reply_text('Meal not found. Use /mine to see your meal IDs.')
        return
    if meal['telegram_user_id'] != update.effective_user.id:
        await update.effective_message.reply_text('Only the person who logged a meal may correct it.')
        return
    if not correction:
        await update.effective_message.reply_text('Describe the correction, e.g. /editlast rice was 150g, not 250g.')
        return
    try:
        result = await analyze(correction, correction=meal, force_log=True)
        if not result.items:
            await update.effective_message.reply_text('Could not recalculate that meal. Try specifying what changed.')
            return
        DB.update_meal(ALLOWED_CHAT_ID, update.effective_user.id, meal['id'],
                       result.title or meal['title'], meal['source_text'] + '\nCorrection: ' + correction,
                       ingredients(result), result.assumptions, result.confidence)
        resp = f"Updated #{meal['id']} · {result.title}\n{macros(DB.totals(ingredients(result)))}"
        if result.assumptions:
            resp += '\nAssumptions: ' + result.assumptions[:450]
        sent = await update.effective_message.reply_text(resp[:3900])
        DB.set_response_message(meal['id'], sent.message_id)
        await sync_past(meal['day'])
    except Exception:
        logger.exception('Correction failed')
        await update.effective_message.reply_text('Could not update this meal. Check the Gemini API and try again.')


async def editlast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await demand_owner(update):
        meal = DB.last(ALLOWED_CHAT_ID, update.effective_user.id)
        await revise_meal(update, meal, ' '.join(context.args).strip())


async def edit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await demand_owner(update):
        return
    if len(context.args) < 2 or not context.args[0].isdigit():
        await update.effective_message.reply_text('Usage: /edit 12 oil was 5g instead of 10g')
        return
    await revise_meal(update, DB.by_id(ALLOWED_CHAT_ID, int(context.args[0])), ' '.join(context.args[1:]))


async def undo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await demand_owner(update):
        return
    last = DB.last(ALLOWED_CHAT_ID, update.effective_user.id)
    if not last:
        await update.effective_message.reply_text('Nothing to undo.')
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
        await update.effective_message.reply_text('Usage: /copy 12 0.75 (copies 75% of meal #12 to YOUR diary)')
        return
    source = DB.by_id(ALLOWED_CHAT_ID, mid)
    if not source:
        await update.effective_message.reply_text('Meal ID not found. Your partner can find it with /mine.')
        return
    items = json.loads(source['ingredients_json'])
    for item in items:
        item['name'] += f' (×{factor:g})'
        for key in ('kcal', 'protein', 'fat', 'carbs'):
            item[key] = round(item[key] * factor, 2)
    try:
        new_id = DB.add_meal(chat_id=ALLOWED_CHAT_ID, user_id=update.effective_user.id,
                             day=now_local().date().isoformat(), now=now_local().isoformat(),
                             title=f"{source['title']} ×{factor:g}", source_text=f'Copy #{mid} ×{factor:g}',
                             source_kind='copy', items=items,
                             assumptions=f'Copied from entry #{mid}; quantities scaled by factor {factor:g}.',
                             confidence=source['confidence'], source_message_id=update.effective_message.message_id)
        response = await update.effective_message.reply_text(
            f"Saved #{new_id} · copied #{mid} ×{factor:g}\n{macros(DB.totals(items))}")
        DB.set_response_message(new_id, response.message_id)
    except Exception:
        logger.exception('Copy failed')
        await update.effective_message.reply_text('Could not copy the meal. Check server logs.')


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
        await update.effective_message.reply_text('Google Sheets not configured yet. Set GOOGLE_SHEET_ID and the service-account credentials on Railway.')
        return False
    return True


async def ingredientadd_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_sheet(update):
        return
    raw = (update.effective_message.text or '').partition(' ')[2].strip()
    fields = [f.strip() for f in raw.split('|')]
    if len(fields) not in (5, 6):
        await update.effective_message.reply_text(
            'Usage (values PER 100g):\n/ingredientadd Творог 1.5% | 73 | 12 | 1.5 | 2.7 | cottage cheese 1.5%, quark 1.5%')
        return
    try:
        def num(value):
            return float(value.replace(',', '.'))
        aliases = tuple(a.strip() for a in fields[5].split(',') if a.strip()) if len(fields) == 6 else ()
        ing = Ingredient(fields[0], aliases, *(num(x) for x in fields[1:5]),
                         updated_at=now_local().isoformat(), updated_by=update.effective_user.first_name)
        status = await asyncio.to_thread(SHEETS.upsert_ingredient, ing)
        await update.effective_message.reply_text(
            f'{ing.name}: {status} in Google Sheet. Per 100 g: {ing.kcal:g} kcal | '
            f'P {ing.protein:g}g | F {ing.fat:g}g | C {ing.carbs:g}g.\n'
            f'Russian/English aliases: {", ".join(ing.aliases) or "none"}.')
    except Exception as exc:
        logger.warning('Ingredient save failed: %s', exc)
        await update.effective_message.reply_text(f'Ingredient not saved: {str(exc)[:450]}')


async def ingredient_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_sheet(update):
        return
    query = ' '.join(context.args).strip()
    if not query:
        await update.effective_message.reply_text('Usage: /ingredient творог 1.5%')
        return
    try:
        found = build_lookup(await asyncio.to_thread(SHEETS.ingredients)).get(normalize_name(query))
        if not found:
            await update.effective_message.reply_text(f'No saved entry for "{query}". Use /ingredients or /ingredientadd.')
            return
        await update.effective_message.reply_text(
            f'{found.name} (per 100g): {found.kcal:g} kcal | P {found.protein:g}g | '
            f'F {found.fat:g}g | C {found.carbs:g}g.\nAliases: {", ".join(found.aliases) or "none"}')
    except Exception:
        logger.exception('Ingredient lookup failed')
        await update.effective_message.reply_text('Could not read Google Sheets right now. Please check /sheetstatus.')


async def ingredients_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_sheet(update):
        return
    try:
        foods = await asyncio.to_thread(SHEETS.ingredients)
        lines = [f'Google Sheet ingredients ({len(foods)}):']
        lines.extend(f'- {x.name} ({x.kcal:g} kcal/100g)' for x in foods[:70])
        if len(foods) > 70:
            lines.append(f'...and {len(foods)-70} more. Search with /ingredient NAME.')
        await update.effective_message.reply_text('\n'.join(lines)[:3900])
    except Exception:
        logger.exception('Ingredients list failed')
        await update.effective_message.reply_text('Could not read Google Sheets. Please check /sheetstatus.')


async def sync_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_sheet(update):
        return
    try:
        if len(context.args) > 1:
            raise ValueError('Use /sync or /sync YYYY-MM-DD')
        if context.args:
            date.fromisoformat(context.args[0])
            days = [context.args[0]]
        else:
            today = now_local().date()
            days = [(today - timedelta(days=1)).isoformat(), today.isoformat()]
        total = sum([await sync_day(day) for day in days])
        await update.effective_message.reply_text(f'Google Sheet updated: {", ".join(days)} ({total} person/day rows). Running this again updates existing rows, not duplicates.')
    except Exception as exc:
        logger.exception('Manual Google Sheets sync failed')
        await update.effective_message.reply_text(f'Sync failed: {str(exc)[:450]}')


async def sheetstatus_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_sheet(update):
        return
    try:
        items = await asyncio.to_thread(SHEETS.ingredients)
        await update.effective_message.reply_text(
            f'Connected to Google Sheet: {len(items)} saved ingredients.\n'
            f'Ingredients tab: {SHEETS.ingredients_tab}\nDaily results tab: {SHEETS.daily_tab}\n'
            'Nightly export: 00:05 Europe/Copenhagen (previous day).')
    except Exception as exc:
        await update.effective_message.reply_text(f'Google Sheet connection failed: {str(exc)[:450]}')


async def normal_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not permitted(update):
        return
    message = update.effective_message
    if not message or not owner(update):
        return
    text = (message.text or message.caption or '').strip()
    # In a shared chat, replying to a bot confirmation with text revises that specific meal.
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
                await message.reply_text('Voice message is too large (12 MB limit).')
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
                await message.reply_text('Media is too large (12 MB limit).')
                return
    except Exception:
        logger.exception('Failed to download Telegram media')
        await message.reply_text('Could not download this media from Telegram. Try again.')
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
        BotCommand('estimate', 'Estimate macros without saving'),
        BotCommand('export', 'Export shared diary to CSV'),
        BotCommand('ingredientadd', 'Save ingredient macros per 100g in Google Sheets'),
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
