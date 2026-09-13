"""Бот магазина: вход в витрину, заявки и вопросы покупателей.

Покупателю бот нужен ради одной кнопки — «Открыть магазин». Владельцу он
заменяет админ-панель: заявка приходит в личку с кнопками «Подтвердить»
и «Отказать», и подтверждение сразу списывает остаток (см. `src/db.py`).

Long polling, а не webhook: работает без белого IP и сертификата, поэтому
один и тот же код запускается и с мака через туннель, и с VPS. Webhook
имеет смысл при тысячах сообщений в минуту, которых у магазина одежды
не будет никогда.

Бот заодно работает почтой между покупателем и владельцем: написанное боту
пересылается владельцу, ответ на пересылку возвращается покупателю. Свою
переписку в базе для этого заводить незачем — кто написал, Telegram хранит
в самом пересланном сообщении.

Тексты для покупателя — на «ты», как принято в Telegram-магазинах, и без
восклицательных знаков в каждой строке.
"""

from __future__ import annotations

import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command, CommandStart
from aiogram.filters.logic import or_f
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonWebApp,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from . import config, db

log = logging.getLogger(__name__)
router = Router(name="магазин")

STATUS_LABEL = {
    "new": "ждёт ответа",
    "confirmed": "подтверждена",
    "rejected": "отклонена",
    "expired": f"без ответа больше {db.STALE_HOURS} часов, вещи вернулись в витрину",
}


# Меню «/» у поля ввода. Пустое меню читается как «бот ничего не умеет»,
# поэтому команды объявляем сами. Владельцу список длиннее — его команды
# в общем списке покупателю только мешали бы.
BUYER_COMMANDS = [
    BotCommand(command="start", description="Открыть магазин"),
    BotCommand(command="help", description="Как заказать"),
]
ADMIN_COMMANDS = BUYER_COMMANDS + [
    BotCommand(command="add", description="Добавить товар"),
    BotCommand(command="items", description="Товары: /items 18 или /items поло"),
    BotCommand(command="orders", description="Последние заявки"),
    BotCommand(command="music", description="Своя песня в витрине"),
    BotCommand(command="cancel", description="Выйти из мастера"),
]

# Те же команды кнопками под полем ввода: набирать «/» с телефона неудобно,
# а меню «/» нужно ещё догадаться открыть. Клавиатура ставится с любым ответом
# бота — тогда она есть и у того, кто написал боту год назад, и у пришедшего
# впервые, а команду руками не приходится набирать ни разу.
BTN_SHOP = "🛍 Магазин"
BTN_HELP = "❓ Как заказать"
BTN_ADD = "➕ Товар"
BTN_ITEMS = "📦 Товары"
BTN_ORDERS = "🧾 Заявки"
BTN_MUSIC = "🎵 Песня"
BTN_CANCEL = "✖️ Отмена"
# Подсказка в поле ввода: она объясняет, что тут ждут кнопку, но не врёт,
# будто писать нельзя, — вопрос текстом уедет владельцу.
PLACEHOLDER = "Жми кнопку ниже — или напиши вопрос"
# Кнопки владельца: их текст обрывает мастер добавления товара так же,
# как команда (см. `admin.command_beats_wizard`).
ADMIN_BUTTONS = frozenset({BTN_ADD, BTN_ITEMS, BTN_ORDERS, BTN_MUSIC, BTN_CANCEL})


def menu_keyboard(settings: config.Settings, admin: bool = False) -> ReplyKeyboardMarkup:
    """Клавиатура под полем ввода. Владельцу — его команды, покупателю — вход.

    Витрина открывается прямо с кнопки (`web_app`), поэтому отдельная инлайн-
    кнопка в /start больше не нужна: тап всё равно один, а места занимает меньше.

    Убрать поле ввода совсем Bot API не позволяет — в чате с ботом оно есть
    всегда. Большая кнопка «Начать» поверх поля — родная кнопка Telegram,
    она показывается сама до первого запуска и обратно не возвращается.
    Поэтому максимум, что тут можно: подсказка в поле и синяя кнопка входа.
    И убирать ввод нам нельзя по делу — вопрос покупателя, набранный текстом,
    бот пересылает владельцу.
    """
    rows = [[KeyboardButton(text=BTN_SHOP, style="primary", web_app=WebAppInfo(url=settings.webapp_url))]]
    if admin:
        rows += [
            [KeyboardButton(text=BTN_ADD), KeyboardButton(text=BTN_ITEMS)],
            [KeyboardButton(text=BTN_ORDERS), KeyboardButton(text=BTN_MUSIC)],
            [KeyboardButton(text=BTN_CANCEL), KeyboardButton(text=BTN_HELP)],
        ]
    else:
        rows.append([KeyboardButton(text=BTN_HELP)])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder=PLACEHOLDER,
    )


async def setup_bot_menu(bot: Bot, settings: config.Settings) -> None:
    """Кнопка рядом с полем ввода и список команд — короткий путь в витрину.

    Ставится при каждом запуске: адрес витрины на отладке меняется вместе
    с туннелем, а забытая кнопка ведёт в никуда и выглядит как поломка.

    Кнопку под полем ввода так не обновить: она живёт в чате и меняется только
    вместе с новым сообщением. Поэтому при смене адреса шлём владельцам свежую
    клавиатуру — иначе их кнопка молча ведёт на выключенный туннель, а выглядит
    это как «магазин не открывается». Что адрес сменился, помнит сам Telegram:
    прошлый запуск оставил его в кнопке-меню. Сверяемся с ней, а не с файлом
    на диске, — диски у мака и у сервера разные, а чат с владельцем один.
    """
    previous = await bot.get_chat_menu_button()
    old_url = getattr(getattr(previous, "web_app", None), "url", settings.webapp_url)
    moved = old_url != settings.webapp_url
    await bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(text="Магазин", web_app=WebAppInfo(url=settings.webapp_url))
    )
    await bot.set_my_commands(BUYER_COMMANDS)
    for admin_id in settings.admin_ids:
        try:
            await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id))
            if moved:
                await bot.send_message(
                    admin_id,
                    "Витрина переехала. Вот свежая кнопка — старая больше не откроется.",
                    reply_markup=menu_keyboard(settings, settings.is_admin(admin_id)),
                )
        except Exception as error:  # noqa: BLE001 — владелец мог ещё не написать боту
            # Не повод не запускаться: команды работают и без подсказки в меню.
            log.warning("до владельца %s не достучались: %s", admin_id, error)


@router.message(CommandStart())
async def start(message: Message, settings: config.Settings) -> None:
    """Старт — это кнопка в витрину, а не приветствие на пол-экрана.

    Открыть мини-приложение за человека Telegram не даёт, поэтому короче
    одного тапа не сделать: убираем всё, что стоит между командой и кнопкой.
    Как заказывать — в /help, когда об этом действительно спросят.
    """
    await message.answer(
        f"<b>{settings.shop_name}</b>\n"
        "Наличие всегда актуальное, проданные вещи пропадают сами.",
        reply_markup=menu_keyboard(settings, settings.is_admin(message.from_user.id)),
    )


@router.message(Command("shop"))
async def open_shop(message: Message, settings: config.Settings) -> None:
    await message.answer(
        "Витрина магазина:",
        reply_markup=menu_keyboard(settings, settings.is_admin(message.from_user.id)),
    )


@router.message(or_f(Command("help"), F.text == BTN_HELP))
async def help_command(message: Message, settings: config.Settings) -> None:
    text = [
        "Как это работает:",
        "1. Открываешь витрину кнопкой ниже.",
        "2. Выбираешь вещь и размер, добавляешь в корзину.",
        "3. Оставляешь заявку с телефоном.",
        "4. Владелец пишет тебе здесь же и подтверждает заказ.",
        "",
        "Вопрос по вещи или заказу — напиши прямо сюда, я передам владельцу.",
    ]
    if settings.is_admin(message.from_user.id):
        text += [
            "",
            "Для владельца — кнопки под полем ввода:",
            f"«{BTN_ADD}» — завести вещь",
            f"«{BTN_ITEMS}» — весь список; нужную ищи набором: «/items 18», «/items поло»",
            f"«{BTN_ORDERS}» — последние заявки",
            f"«{BTN_MUSIC}» — своя песня в витрине",
            "",
            "На пересланный вопрос покупателя отвечай ответом (reply) — "
            "бот доставит ответ ему от имени магазина.",
        ]
    await message.answer(
        "\n".join(text),
        reply_markup=menu_keyboard(settings, settings.is_admin(message.from_user.id)),
    )


def order_text(order: dict, settings: config.Settings) -> str:
    """Заявка одним сообщением: всё, что нужно, чтобы ответить не переспрашивая.

    Всё, что писал человек, экранируется: сообщения уходят с parse_mode=HTML,
    и одна угловая скобка в имени или комментарии — это отказ Telegram принять
    сообщение, то есть заявка, о которой владелец не узнает.
    """
    lines = [f"🧾 <b>Заявка №{order['id']}</b> · {order['total']:,} {settings.currency}".replace(",", " ")]
    lines.append("")
    who = escape(order["customer_name"]) or "без имени"
    if order["username"]:
        who += f" (@{escape(order['username'])})"
    lines.append(f"Покупатель: {who}")
    lines.append(f"Телефон: {escape(order['phone'])}")
    if order["delivery"]:
        lines.append(f"Доставка: {escape(order['delivery'])}")
    if order["address"]:
        lines.append(f"Адрес: {escape(order['address'])}")
    if order["comment"]:
        lines.append(f"Комментарий: {escape(order['comment'])}")
    lines.append("")
    for item in order["items"]:
        size = "" if item["size"] == db.ONE_SIZE else f" · размер {escape(item['size'])}"
        count = "" if item["quantity"] == 1 else f" · {item['quantity']} шт"
        price = f"{item['price']:,}".replace(",", " ")
        lines.append(f"• {escape(item['title'])}{size}{count} — {price} {settings.currency}")
    if order["status"] != "new":
        lines.append("")
        lines.append(f"Статус: {STATUS_LABEL.get(order['status'], order['status'])}")
    return "\n".join(lines)


def order_keyboard(order_id: int, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"order:confirm:{order_id}"),
                InlineKeyboardButton(text="✖️ Отказать", callback_data=f"order:reject:{order_id}"),
            ],
            [InlineKeyboardButton(text="💬 Написать покупателю", url=f"tg://user?id={user_id}")],
        ]
    )


async def notify_admins(bot: Bot, settings: config.Settings, order_id: int) -> None:
    """Отправляет заявку владельцу. Вызывается из веб-части при оформлении."""
    order = db.get_order(order_id)
    if not order:
        return
    text = order_text(order, settings)
    keyboard = order_keyboard(order_id, order["user_id"])
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, text, reply_markup=keyboard)
        except Exception as error:  # noqa: BLE001 — один недоступный админ не должен ронять заявку
            log.warning("заявка №%s не дошла до %s: %s", order_id, admin_id, error)


async def _show_order(callback: CallbackQuery, text: str) -> None:
    """Переписывает сообщение с заявкой её новым состоянием.

    Правка может не пройти: сообщение старше двух суток или Telegram считает
    текст неизменившимся. Заявка при этом уже закрыта в базе, и уронить на этом
    обработчик нельзя — иначе покупатель не получит ответа, которого ждёт.
    """
    try:
        await callback.message.edit_text(text)
    except Exception as error:  # noqa: BLE001 — решение по заявке уже принято
        log.info("не смог переписать сообщение заявки: %s", error)


@router.callback_query(F.data.startswith("order:"))
async def decide_order(callback: CallbackQuery, bot: Bot, settings: config.Settings) -> None:
    if not settings.is_admin(callback.from_user.id):
        await callback.answer("Это кнопки владельца магазина", show_alert=True)
        return

    _, action, raw_id = callback.data.split(":", 2)
    order_id = int(raw_id)
    status = "confirmed" if action == "confirm" else "rejected"
    order = db.set_order_status(order_id, status)

    if order is None:
        # Заявку уже закрыли — например, со второго устройства или она протухла.
        await callback.answer("Заявка уже закрыта", show_alert=True)
        current = db.get_order(order_id)
        if current:
            await _show_order(callback, order_text(current, settings))
        return

    await _show_order(callback, order_text(order, settings))
    await callback.answer("Подтверждено" if status == "confirmed" else "Отклонено")

    # Покупателю пишет бот: человек оставил заявку и ждёт ответа именно здесь.
    try:
        if status == "confirmed":
            await bot.send_message(
                order["user_id"],
                f"Заявка №{order_id} подтверждена — вещи отложены.\n"
                "Владелец магазина напишет, чтобы договориться об оплате и доставке.",
                reply_markup=menu_keyboard(settings, settings.is_admin(order["user_id"])),
            )
        else:
            await bot.send_message(
                order["user_id"],
                f"По заявке №{order_id} не сложилось: этих вещей уже нет в наличии.\n"
                "Загляни в витрину — там появляется новое.",
                reply_markup=menu_keyboard(settings, settings.is_admin(order["user_id"])),
            )
    except Exception as error:  # noqa: BLE001 — покупатель мог заблокировать бота
        log.info("не смог написать покупателю %s: %s", order["user_id"], error)


@router.message(or_f(Command("orders"), F.text == BTN_ORDERS))
async def recent_orders(message: Message, settings: config.Settings) -> None:
    if not settings.is_admin(message.from_user.id):
        return
    recent = db.orders(limit=10)
    if not recent:
        await message.answer("Заявок пока не было.")
        return
    for order in recent:
        full = db.get_order(order["id"])
        keyboard = order_keyboard(order["id"], order["user_id"]) if order["status"] == "new" else None
        await message.answer(order_text(full, settings), reply_markup=keyboard)


# --- вопросы покупателей -----------------------------------------------------
#
# Написанное боту раньше не доходило никуда: покупатель спрашивал про размер
# и оставался без ответа. Своей переписки в базе для этого не нужно — вопрос
# пересылается владельцу, а в пересланном сообщении Telegram сам держит, кто
# его написал: по этой отметке ответ и уходит обратно. Так владелец отвечает
# из чата с ботом, не отдавая покупателю свой личный аккаунт.


@router.message(F.reply_to_message.forward_origin)
async def answer_customer(message: Message, settings: config.Settings) -> None:
    """Ответ владельца на пересланный вопрос — покупателю, от имени магазина."""
    if not settings.is_admin(message.from_user.id):
        # Покупатель тоже может ответить на пересланное им же сообщение —
        # это обычный вопрос, и он должен уйти дальше, к пересылке владельцу.
        raise SkipHandler

    customer = getattr(message.reply_to_message.forward_origin, "sender_user", None)
    if customer is None:
        # У покупателя закрыты пересылки: Telegram не говорит, от кого сообщение.
        await message.answer(
            "Не вижу, кому отвечать: покупатель скрыл пересылку. "
            "Написать ему можно кнопкой под его заявкой."
        )
        return

    try:
        # Копией, а не пересылкой: покупателю пишет магазин, и подпись владельца
        # в чужой личке ему не нужна.
        await message.copy_to(customer.id)
    except Exception as error:  # noqa: BLE001 — покупатель мог заблокировать бота
        log.info("ответ покупателю %s не дошёл: %s", customer.id, error)
        await message.answer("Не доставил: похоже, покупатель заблокировал бота.")
    else:
        await message.answer("Отправил покупателю.")


@router.message()
async def ask_seller(message: Message, settings: config.Settings) -> None:
    """Любое другое сообщение — вопрос владельцу магазина."""
    if settings.is_admin(message.from_user.id):
        return
    if (message.text or "").startswith("/"):
        # Команду, которой у бота нет, набрал не покупатель с вопросом, а тот,
        # кто подобрал её на слух. Владельцу это приходило бы как «/add» от
        # незнакомого человека — шум, на который нечего ответить.
        return

    delivered = False
    for admin_id in settings.admin_ids:
        try:
            await message.forward(admin_id)
            delivered = True
        except Exception as error:  # noqa: BLE001 — один недоступный админ не теряет вопрос
            log.warning("вопрос от %s не дошёл до %s: %s", message.from_user.id, admin_id, error)

    await message.answer(
        "Передал владельцу магазина — он ответит здесь же."
        if delivered
        else "Не получилось передать вопрос. Попробуй ещё раз чуть позже.",
        reply_markup=menu_keyboard(settings),
    )
