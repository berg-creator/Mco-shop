"""Бот магазина: вход в витрину и работа с заявками.

Покупателю бот нужен ради одной кнопки — «Открыть магазин». Владельцу он
заменяет админ-панель: заявка приходит в личку с кнопками «Подтвердить»
и «Отказать», и подтверждение сразу списывает остаток (см. `src/db.py`).

Long polling, а не webhook: работает без белого IP и сертификата, поэтому
один и тот же код запускается и с мака через туннель, и с VPS. Webhook
имеет смысл при тысячах сообщений в минуту, которых у магазина одежды
не будет никогда.

Тексты для покупателя — на «ты», как принято в Telegram-магазинах, и без
восклицательных знаков в каждой строке.
"""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)

from . import config, db

log = logging.getLogger(__name__)
router = Router(name="магазин")

STATUS_LABEL = {
    "new": "ждёт ответа",
    "confirmed": "подтверждена",
    "rejected": "отклонена",
}


def shop_keyboard(settings: config.Settings) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="🛍 Открыть магазин",
                web_app=WebAppInfo(url=settings.webapp_url),
            )]
        ]
    )


async def setup_menu_button(bot: Bot, settings: config.Settings) -> None:
    """Кнопка рядом с полем ввода — самый короткий путь в витрину.

    Ставится при каждом запуске: адрес витрины на отладке меняется вместе
    с туннелем, а забытая кнопка ведёт в никуда и выглядит как поломка.
    """
    await bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(text="Магазин", web_app=WebAppInfo(url=settings.webapp_url))
    )


@router.message(CommandStart())
async def start(message: Message, settings: config.Settings) -> None:
    await message.answer(
        f"<b>{settings.shop_name}</b>\n\n"
        "Витрина открывается прямо здесь: наличие всегда актуальное, "
        "проданные вещи пропадают сами.\n\n"
        "Выбери вещь, добавь в корзину и оставь заявку — я напишу, "
        "чтобы подтвердить наличие и договориться об оплате.",
        reply_markup=shop_keyboard(settings),
    )


@router.message(Command("shop"))
async def open_shop(message: Message, settings: config.Settings) -> None:
    await message.answer("Витрина магазина:", reply_markup=shop_keyboard(settings))


@router.message(Command("help"))
async def help_command(message: Message, settings: config.Settings) -> None:
    text = [
        "Как это работает:",
        "1. Открываешь витрину кнопкой ниже.",
        "2. Выбираешь вещь и размер, добавляешь в корзину.",
        "3. Оставляешь заявку с телефоном.",
        "4. Владелец пишет тебе здесь же и подтверждает заказ.",
    ]
    if settings.is_admin(message.from_user.id):
        text += [
            "",
            "Для владельца:",
            "/add — добавить товар",
            "/items — список товаров, остатки, скрыть или удалить",
            "/orders — последние заявки",
        ]
    await message.answer("\n".join(text), reply_markup=shop_keyboard(settings))


def order_text(order: dict, settings: config.Settings) -> str:
    """Заявка одним сообщением: всё, что нужно, чтобы ответить не переспрашивая."""
    lines = [f"🧾 <b>Заявка №{order['id']}</b> · {order['total']:,} {settings.currency}".replace(",", " ")]
    lines.append("")
    who = order["customer_name"] or "без имени"
    if order["username"]:
        who += f" (@{order['username']})"
    lines.append(f"Покупатель: {who}")
    lines.append(f"Телефон: {order['phone']}")
    if order["delivery"]:
        lines.append(f"Доставка: {order['delivery']}")
    if order["address"]:
        lines.append(f"Адрес: {order['address']}")
    if order["comment"]:
        lines.append(f"Комментарий: {order['comment']}")
    lines.append("")
    for item in order["items"]:
        size = "" if item["size"] == db.ONE_SIZE else f" · размер {item['size']}"
        count = "" if item["quantity"] == 1 else f" · {item['quantity']} шт"
        price = f"{item['price']:,}".replace(",", " ")
        lines.append(f"• {item['title']}{size}{count} — {price} {settings.currency}")
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
        # Заявку уже закрыли — например, со второго устройства.
        await callback.answer("Заявка уже закрыта", show_alert=True)
        current = db.get_order(order_id)
        if current:
            await callback.message.edit_text(order_text(current, settings))
        return

    await callback.message.edit_text(order_text(order, settings))
    await callback.answer("Подтверждено" if status == "confirmed" else "Отклонено")

    # Покупателю пишет бот: человек оставил заявку и ждёт ответа именно здесь.
    try:
        if status == "confirmed":
            await bot.send_message(
                order["user_id"],
                f"Заявка №{order_id} подтверждена — вещи отложены.\n"
                "Владелец магазина напишет, чтобы договориться об оплате и доставке.",
            )
        else:
            await bot.send_message(
                order["user_id"],
                f"По заявке №{order_id} не сложилось: этих вещей уже нет в наличии.\n"
                "Загляни в витрину — там появляется новое.",
                reply_markup=shop_keyboard(settings),
            )
    except Exception as error:  # noqa: BLE001 — покупатель мог заблокировать бота
        log.info("не смог написать покупателю %s: %s", order["user_id"], error)


@router.message(Command("orders"))
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
