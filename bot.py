import asyncio
import logging
import os
import re
import sqlite3
from contextlib import closing
from html import unescape
from typing import Optional
from urllib.parse import urlencode, urljoin

import requests
from bs4 import BeautifulSoup
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DB = "olx_bot.db"
HTTP_TIMEOUT = 20

# ---------- Conversation states ----------
ADD_KEYWORD, ADD_MAX_PRICE, ADD_COND, ADD_WHERE, ADD_CITY = range(5)

# ---------- Menus ----------
MAIN_MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton("➕ Добавить поиск"), KeyboardButton("📋 Мои подписки")],
        [KeyboardButton("🔎 Проверить всё сейчас"), KeyboardButton("❓ Помощь")],
    ],
    resize_keyboard=True,
)

COND_KB = ReplyKeyboardMarkup(
    [
        [KeyboardButton("Любое"), KeyboardButton("Новое"), KeyboardButton("Б/у")],
        [KeyboardButton("Отмена")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)

WHERE_KB = ReplyKeyboardMarkup(
    [
        [KeyboardButton("Только заголовок"), KeyboardButton("Только текст")],
        [KeyboardButton("Заголовок + текст")],
        [KeyboardButton("Отмена")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)

YES_SKIP_KB = ReplyKeyboardMarkup(
    [
        [KeyboardButton("Пропустить")],
        [KeyboardButton("Отмена")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)



# ---------- DB ----------
def db_connect():
    return sqlite3.connect(DB)


def init_db():
    with closing(db_connect()) as conn:
        c = conn.cursor()
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS subs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                keyword TEXT NOT NULL,
                max_price INTEGER,
                cond TEXT NOT NULL DEFAULT 'any',
                where_field TEXT NOT NULL DEFAULT 'title',
                city TEXT,
                interval INTEGER NOT NULL DEFAULT 300,
                active INTEGER NOT NULL DEFAULT 1,
                last_checked INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS seen (
                sub_id INTEGER NOT NULL,
                url TEXT NOT NULL,
                created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
                PRIMARY KEY (sub_id, url)
            )
            """
        )
        conn.commit()


def add_subscription(
    user_id: int,
    keyword: str,
    max_price: Optional[int],
    cond: str,
    where_field: str,
    city: Optional[str],
    interval: int,
):
    with closing(db_connect()) as conn:
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO subs(user_id, keyword, max_price, cond, where_field, city, interval, active)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (user_id, keyword, max_price, cond, where_field, city, interval),
        )
        conn.commit()
        return c.lastrowid


def get_user_subs(user_id: int):
    with closing(db_connect()) as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT id, keyword, max_price, cond, where_field, city, interval, active, last_checked
            FROM subs
            WHERE user_id=?
            ORDER BY id DESC
            """,
            (user_id,),
        )
        return c.fetchall()


def get_sub(sub_id: int):
    with closing(db_connect()) as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT id, user_id, keyword, max_price, cond, where_field, city, interval, active, last_checked
            FROM subs
            WHERE id=?
            """,
            (sub_id,),
        )
        return c.fetchone()


def set_sub_active(sub_id: int, active: int):
    with closing(db_connect()) as conn:
        c = conn.cursor()
        c.execute("UPDATE subs SET active=? WHERE id=?", (active, sub_id))
        conn.commit()


def delete_sub(sub_id: int):
    with closing(db_connect()) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM seen WHERE sub_id=?", (sub_id,))
        c.execute("DELETE FROM subs WHERE id=?", (sub_id,))
        conn.commit()


def update_last_checked(sub_id: int, ts: int):
    with closing(db_connect()) as conn:
        c = conn.cursor()
        c.execute("UPDATE subs SET last_checked=? WHERE id=?", (ts, sub_id))
        conn.commit()


def has_seen(sub_id: int, url: str) -> bool:
    with closing(db_connect()) as conn:
        c = conn.cursor()
        c.execute("SELECT 1 FROM seen WHERE sub_id=? AND url=?", (sub_id, url))
        return c.fetchone() is not None


def mark_seen(sub_id: int, url: str):
    with closing(db_connect()) as conn:
        c = conn.cursor()
        c.execute(
            "INSERT OR IGNORE INTO seen(sub_id, url) VALUES (?, ?)",
            (sub_id, url),
        )
        conn.commit()


def get_due_subs(now_ts: int):
    with closing(db_connect()) as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT id, user_id, keyword, max_price, cond, where_field, city, interval, active, last_checked
            FROM subs
            WHERE active=1
            """
        )
        rows = c.fetchall()

    due = []
    for row in rows:
        interval = row[7]
        last_checked = row[9]
        if now_ts - last_checked >= interval:
            due.append(row)
    return due


# ---------- Helpers ----------
def escape_md(text: str) -> str:
    if text is None:
        return ""
    chars = r"_*[]()~`>#+-=|{}.!"
    for ch in chars:
        text = text.replace(ch, f"\\{ch}")
    return text


def parse_price_to_int(price_text: str) -> int:
    digits = re.sub(r"[^\d]", "", price_text or "")
    return int(digits) if digits else 0


def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def main_menu_text() -> str:
    return (
        "Что умею:\n"
        "• добавить поиск по OLX\n"
        "• следить за новыми объявлениями\n"
        "• фильтр по цене\n"
        "• фильтр по состоянию\n"
        "• искать ключ в заголовке или тексте"
    )


def sub_to_dict(row):
    return {
        "id": row[0],
        "user_id": row[1],
        "keyword": row[2],
        "max_price": row[3],
        "cond": row[4],
        "where_field": row[5],
        "city": row[6],
        "interval": row[7],
        "active": row[8],
        "last_checked": row[9],
    }


def cond_label(cond: str) -> str:
    return {"any": "любое", "new": "новое", "used": "б/у"}.get(cond, cond)


def where_label(where_field: str) -> str:
    return {
        "title": "только заголовок",
        "text": "только текст",
        "both": "заголовок + текст",
    }.get(where_field, where_field)


def sub_card_text(sub: dict) -> str:
    status = "🟢 активна" if sub["active"] else "⏸ пауза"
    parts = [
        f"*Подписка #{sub['id']}* — {status}",
        f"*Ключ:* {escape_md(sub['keyword'])}",
        f"*Цена до:* {escape_md(str(sub['max_price'])) if sub['max_price'] else 'без лимита'}",
        f"*Состояние:* {escape_md(cond_label(sub['cond']))}",
        f"*Где искать:* {escape_md(where_label(sub['where_field']))}",
        f"*Город:* {escape_md(sub['city']) if sub['city'] else 'любой'}",
        f"*Интервал:* {sub['interval']} сек",
    ]
    return "\n".join(parts)


def sub_actions_kb(sub_id: int, active: int):
    row1 = [
        InlineKeyboardButton("🔎 Проверить", callback_data=f"check:{sub_id}"),
        InlineKeyboardButton("🗑 Удалить", callback_data=f"delete:{sub_id}"),
    ]
    if active:
        row2 = [InlineKeyboardButton("⏸ Пауза", callback_data=f"pause:{sub_id}")]
    else:
        row2 = [InlineKeyboardButton("▶️ Запустить", callback_data=f"resume:{sub_id}")]
    return InlineKeyboardMarkup([row1, row2])


# ---------- OLX parsing ----------
def build_olx_search_url(sub: dict, page: int = 1) -> str:
    params = {
        "q": sub["keyword"],
        "page": page,
        "search[order]": "created_at:desc",  # новые сначала
    }

    if sub["max_price"]:
        params["search[filter_float_price:to]"] = sub["max_price"]

    if sub["cond"] == "new":
        params["search[filter_enum_state][0]"] = "new"
    elif sub["cond"] == "used":
        params["search[filter_enum_state][0]"] = "used"

    base = "https://www.olx.ua/uk/list/"
    return f"{base}?{urlencode(params, doseq=True)}"


import random

def http_get(url: str) -> requests.Response:
    headers = {
        "User-Agent": random.choice([
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0",
        ])
    }

    # лёгкая рандомная задержка
    import time
    time.sleep(random.uniform(0.5, 1.2))

    return requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)


def parse_listing_cards(html: str):
    soup = BeautifulSoup(html, "html.parser")
    items = []

    # У OLX верстка плавает, поэтому несколько попыток.
    cards = soup.select("a[href*='/d/obyavlenie/'], a[href*='/d/uk/obyavlenie/']")
    seen_urls = set()

    for a in cards:
        href = a.get("href")
        if not href:
            continue

        href = urljoin("https://www.olx.ua", href.split("#")[0].split("?")[0])

        if href in seen_urls:
            continue
        seen_urls.add(href)

        title = ""
        price = ""
        location = ""
        snippet = ""

        h6 = a.select_one("h4, h5, h6")
        if h6:
            title = normalize_spaces(h6.get_text(" ", strip=True))

        price_el = a.select_one("[data-testid='ad-price'], p")
        if price_el:
            price = normalize_spaces(price_el.get_text(" ", strip=True))

        # Иногда родитель содержит ещё текст
        parent_text = normalize_spaces(a.get_text(" ", strip=True))
        if not title and parent_text:
            title = parent_text[:180]

        # Попытка вытащить локацию / сниппет рядом
        parent = a.parent
        if parent:
            text = normalize_spaces(parent.get_text(" ", strip=True))
            snippet = text[:300]

        if not title:
            continue

        items.append(
            {
                "title": unescape(title),
                "price": price or "Цена не указана",
                "price_num": parse_price_to_int(price),
                "url": href,
                "location": location,
                "snippet": snippet,
            }
        )

    return items


def fetch_item_description(url: str) -> str:
    try:
        r = http_get(url)
        soup = BeautifulSoup(r.text, "html.parser")

        selectors = [
            '[data-cy="ad_description"]',
            '[data-testid="ad-description-container"]',
            '[data-testid="description-content"]',
            "div[data-testid='description']",
        ]

        for sel in selectors:
            el = soup.select_one(sel)
            if el:
                return normalize_spaces(el.get_text(" ", strip=True))

        # fallback
        body = normalize_spaces(soup.get_text(" ", strip=True))
        return body[:3000]
    except Exception as e:
        logger.warning("Description fetch failed for %s: %s", url, e)
        return ""


def item_matches(item: dict, sub: dict) -> bool:
    if sub["max_price"] and item["price_num"] and item["price_num"] > sub["max_price"]:
        return False

    if sub["city"]:
        whole_text = f"{item.get('title', '')} {item.get('snippet', '')} {item.get('location', '')}".lower()
        if sub["city"].lower() not in whole_text:
            return False

    keyword = sub["keyword"].lower()

    if sub["where_field"] == "title":
        return keyword in (item.get("title") or "").lower()

    if sub["where_field"] == "text":
        desc = fetch_item_description(item["url"])
        return keyword in desc.lower()

    if sub["where_field"] == "both":
        if keyword in (item.get("title") or "").lower():
            return True
        desc = fetch_item_description(item["url"])
        return keyword in desc.lower()

    return False


def fetch_matching_items(sub: dict):
    all_items = []

    # проверяем 3 страницы
    for page in range(1, 4):
        try:
            url = build_olx_search_url(sub, page)
            r = http_get(url)
            items = parse_listing_cards(r.text)

            for item in items:
                if item_matches(item, sub):
                    all_items.append(item)

        except Exception as e:
            logger.warning(f"Ошибка страницы {page}: {e}")

    return all_items


# ---------- Background checker ----------
async def send_new_item(app: Application, sub: dict, item: dict):
    text = (
        f"🆕 *Новое объявление*\n\n"
        f"*Подписка:* #{sub['id']}\n"
        f"*Ключ:* {escape_md(sub['keyword'])}\n"
        f"*Заголовок:* {escape_md(item['title'])}\n"
        f"*Цена:* {escape_md(item['price'])}\n"
        f"*Ссылка:* {escape_md(item['url'])}"
    )
    await app.bot.send_message(
        chat_id=sub["user_id"],
        text=text,
        parse_mode=ParseMode.MARKDOWN_V2,
        disable_web_page_preview=False,
    )


async def run_check_for_sub(app: Application, sub: dict, notify_if_empty=False):
    found = 0
    new_count = 0

    try:
        items = await asyncio.to_thread(fetch_matching_items, sub)
        found = len(items)

        for item in items:
            if has_seen(sub["id"], item["url"]):
                continue

            mark_seen(sub["id"], item["url"])
            await send_new_item(app, sub, item)
            new_count += 1

    except Exception as e:
        logger.exception("Check failed for sub %s", sub["id"])
        try:
            await app.bot.send_message(
                chat_id=sub["user_id"],
                text=f"Ошибка проверки подписки #{sub['id']}: {e}",
            )
        except Exception:
            pass
    finally:
        now_ts = int(asyncio.get_running_loop().time())
        # monotonic time для loop.time не годится в БД логически, но для интервальной проверки подходит.
        # Чтобы было проще и стабильно, запишем обычный epoch:
        import time
        update_last_checked(sub["id"], int(time.time()))

    if notify_if_empty and new_count == 0:
        return f"Проверено. Совпадений найдено: {found}. Новых: 0"
    return f"Проверено. Совпадений: {found}. Новых: {new_count}"


async def periodic_checker(app: Application):
    import time

    await asyncio.sleep(3)

    while True:
        try:
            now_ts = int(time.time())
            rows = get_due_subs(now_ts)

            for row in rows:
                sub = sub_to_dict(row)
                await run_check_for_sub(app, sub, notify_if_empty=False)
                await asyncio.sleep(0.5)

        except Exception:
            logger.exception("Periodic checker crashed")

        await asyncio.sleep(5)


async def post_init(app: Application):
    app.create_task(periodic_checker(app))


# ---------- Commands / menu ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(main_menu_text(), reply_markup=MAIN_MENU)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Нажимай кнопки.\n\n"
        "Сценарий:\n"
        "1. Жмёшь «➕ Добавить поиск»\n"
        "2. Вводишь ключ\n"
        "3. Ставишь лимит цены\n"
        "4. Выбираешь состояние\n"
        "5. Выбираешь, где искать ключ\n"
        "6. Ставишь интервал\n"
        "7. Готово\n\n"
        "В «📋 Мои подписки» будут кнопки:\n"
        "• проверить\n"
        "• пауза / запуск\n"
        "• удалить"
    )
    await update.message.reply_text(text, reply_markup=MAIN_MENU)


async def menu_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    if text == "➕ Добавить поиск":
        await update.message.reply_text(
            "Введи ключевое слово.\nПример: iphone 13",
            reply_markup=ReplyKeyboardMarkup([[KeyboardButton("Отмена")]], resize_keyboard=True),
        )
        return ADD_KEYWORD

    if text == "📋 Мои подписки":
        return await show_subscriptions(update, context)

    if text == "🔎 Проверить всё сейчас":
        await update.message.reply_text("Проверяю все активные подписки...")
        rows = get_user_subs(update.effective_user.id)
        count = 0
        for row in rows:
            sub = {
                "id": row[0],
                "user_id": update.effective_user.id,
                "keyword": row[1],
                "max_price": row[2],
                "cond": row[3],
                "where_field": row[4],
                "city": row[5],
                "interval": row[6],
                "active": row[7],
                "last_checked": row[8],
            }
            if sub["active"]:
                await run_check_for_sub(context.application, sub, notify_if_empty=False)
                count += 1
                await asyncio.sleep(1)

        await update.message.reply_text(f"Готово. Проверено подписок: {count}", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    if text == "❓ Помощь":
        await help_cmd(update, context)
        return ConversationHandler.END

    await update.message.reply_text("Жми кнопку из меню.", reply_markup=MAIN_MENU)
    return ConversationHandler.END


async def show_subscriptions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_user_subs(update.effective_user.id)
    if not rows:
        await update.message.reply_text("Подписок пока нет.", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    await update.message.reply_text("Твои подписки:", reply_markup=MAIN_MENU)

    for row in rows:
        sub_id = row[0]
        keyword = row[1]
        max_price = row[2]
        cond = row[3]
        where_field = row[4]
        city = row[5]
        interval = row[6]
        active = row[7]

        status = "активна" if active else "пауза"
        text = (
            f"Подписка #{sub_id}\n"
            f"Ключ: {keyword}\n"
            f"Цена до: {max_price if max_price else 'без лимита'}\n"
            f"Состояние: {cond}\n"
            f"Где искать: {where_field}\n"
            f"Город: {city if city else 'любой'}\n"
            f"Интервал: {interval} сек\n"
            f"Статус: {status}"
        )

        await update.message.reply_text(
            text,
            reply_markup=sub_actions_kb(sub_id, active),
        )

    return ConversationHandler.END


# ---------- Add flow ----------
async def add_keyword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text == "Отмена":
        await update.message.reply_text("Отменено.", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    context.user_data["new_sub"] = {"keyword": text}
    await update.message.reply_text(
        "Максимальная цена?\nВведи число или нажми «Пропустить».",
        reply_markup=YES_SKIP_KB,
    )
    return ADD_MAX_PRICE


async def add_max_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    if text == "Отмена":
        await update.message.reply_text("Отменено.", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    if text == "Пропустить":
        context.user_data["new_sub"]["max_price"] = None
    else:
        if not text.isdigit():
            await update.message.reply_text("Нужно число. Пример: 15000")
            return ADD_MAX_PRICE
        context.user_data["new_sub"]["max_price"] = int(text)

    await update.message.reply_text(
        "Состояние товара?",
        reply_markup=COND_KB,
    )
    return ADD_COND


async def add_cond(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    if text == "Отмена":
        await update.message.reply_text("Отменено.", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    mapping = {"Любое": "any", "Новое": "new", "Б/у": "used"}
    if text not in mapping:
        await update.message.reply_text("Выбери кнопку: Любое / Новое / Б/у")
        return ADD_COND

    context.user_data["new_sub"]["cond"] = mapping[text]
    await update.message.reply_text(
        "Где искать ключ?",
        reply_markup=WHERE_KB,
    )
    return ADD_WHERE


async def add_where(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    if text == "Отмена":
        await update.message.reply_text("Отменено.", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    mapping = {
        "Только заголовок": "title",
        "Только текст": "text",
        "Заголовок + текст": "both",
    }
    if text not in mapping:
        await update.message.reply_text("Выбери кнопку.")
        return ADD_WHERE

    context.user_data["new_sub"]["where_field"] = mapping[text]
    await update.message.reply_text(
        "Город? Введи текст или нажми «Пропустить».\nПример: Днепр",
        reply_markup=YES_SKIP_KB,
    )
    return ADD_CITY


async def add_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    if text == "Отмена":
        await update.message.reply_text("Отменено.", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    # сохраняем город
    context.user_data["new_sub"]["city"] = None if text == "Пропустить" else text

    data = context.user_data.get("new_sub", {})

    sub_id = add_subscription(
        user_id=update.effective_user.id,
        keyword=data["keyword"],
        max_price=data.get("max_price"),
        cond=data["cond"],
        where_field=data["where_field"],
        city=data.get("city"),
        interval=10,
    )

    context.user_data.pop("new_sub", None)

    await update.message.reply_text(
        f"Готово. Подписка #{sub_id} создана (10 сек).",
        reply_markup=MAIN_MENU,
    )

    return ConversationHandler.END





async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("new_sub", None)
    await update.message.reply_text("Отменено.", reply_markup=MAIN_MENU)
    return ConversationHandler.END


# ---------- Inline buttons ----------
async def inline_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    action, raw_id = data.split(":")
    sub_id = int(raw_id)

    row = get_sub(sub_id)
    if not row:
        await query.edit_message_text("Подписка не найдена.")
        return

    sub = sub_to_dict(row)

    if query.from_user.id != sub["user_id"]:
        await query.answer("Это не твоя подписка.", show_alert=True)
        return

    if action == "pause":
        set_sub_active(sub_id, 0)
        sub["active"] = 0
        await query.edit_message_text(
            sub_card_text(sub),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=sub_actions_kb(sub_id, 0),
        )
        return

    if action == "resume":
        set_sub_active(sub_id, 1)
        sub["active"] = 1
        await query.edit_message_text(
            sub_card_text(sub),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=sub_actions_kb(sub_id, 1),
        )
        return

    if action == "delete":
        delete_sub(sub_id)
        await query.edit_message_text(f"Подписка #{sub_id} удалена.")
        return

    if action == "check":
        await query.answer("Проверяю...")
        result = await run_check_for_sub(context.application, sub, notify_if_empty=True)
        await query.message.reply_text(result)
        return


# ---------- Main ----------
def build_app(token: str) -> Application:
    conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^➕ Добавить поиск$"), menu_text_router),
            MessageHandler(filters.Regex("^📋 Мои подписки$"), menu_text_router),
            MessageHandler(filters.Regex("^🔎 Проверить всё сейчас$"), menu_text_router),
            MessageHandler(filters.Regex("^❓ Помощь$"), menu_text_router),
        ],
        states={
            ADD_KEYWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_keyword)],
            ADD_MAX_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_max_price)],
            ADD_COND: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_cond)],
            ADD_WHERE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_where)],
            ADD_CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_city)],
            
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Regex("^Отмена$"), cancel),
        ],
        per_message=False,
    )

    app = (
        ApplicationBuilder()
        .token(token)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(inline_actions, pattern=r"^(pause|resume|delete|check):\d+$"))

    return app


def main():
    init_db()

    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("Не задан BOT_TOKEN")

    # Для Python 3.14 это часто лечит ошибку с event loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = build_app(token)
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()