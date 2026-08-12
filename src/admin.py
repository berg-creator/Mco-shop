"""Админка внутри бота: товар заводится с телефона за минуту.

Владелец магазина не будет править JSON и не станет заходить в веб-панель
с ноутбука — он фотографирует вещь и тут же выкладывает её. Поэтому добавление
устроено как разговор: фото → название → бренд → категория → цена → размеры →
описание. На любом шаге можно написать «-» и пропустить, «отмена» — выйти.

Черновик живёт в памяти процесса (MemoryStorage): перезапуск сервера теряет
незаконченную карточку. Это осознанно — хранить недописанные черновики в базе
значит завести им жизненный цикл и чистку, а карточка заводится за минуту.

Фотографии скачиваются в `data/photos` сразу: держать `file_id` и ходить за
картинкой в Telegram на каждый показ витрины — лишняя задержка и лишний способ
всё сломать, когда токен сменится.
"""

from __future__ import annotations

import logging
from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from . import config, db

log = logging.getLogger(__name__)

MAX_PHOTOS = 5
SKIP = {"-", "нет", "пропустить", "далее"}
CANCEL = {"отмена", "стоп", "/cancel"}


class IsAdmin(BaseFilter):
    """Пускает только владельцев магазина из ADMIN_IDS."""

    async def __call__(self, event: TelegramObject, settings: config.Settings) -> bool:
        user = getattr(event, "from_user", None)
        return bool(user and settings.is_admin(user.id))


router = Router(name="админка")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


class NewProduct(StatesGroup):
    photos = State()
    name = State()
    brand = State()
    category = State()
    price = State()
    sizes = State()
    description = State()


class EditStock(StatesGroup):
    value = State()


def _done_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Фото загружены →", callback_data="add:photos_done")]]
    )


def _categories_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=category["name"], callback_data=f"add:cat:{category['id']}")]
        for category in db.categories()
    ]
    rows.append([InlineKeyboardButton(text="Без категории", callback_data="add:cat:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменил.")


@router.message(Command("add"))
async def add_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(NewProduct.photos)
    await state.update_data(photos=[])
    await message.answer(
        "Новый товар.\n\n"
        f"Пришли фото вещи — до {MAX_PHOTOS} штук, можно пачкой. "
        "Когда закончишь, нажми кнопку. Если фото нет — напиши «-».\n\n"
        "Выйти из режима: «отмена».",
        reply_markup=_done_keyboard(),
    )


@router.message(NewProduct.photos, F.photo)
async def add_photo(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    photos: list[str] = data.get("photos", [])
    if len(photos) >= MAX_PHOTOS:
        await message.answer(f"Больше {MAX_PHOTOS} фото не нужно, нажми кнопку ниже.")
        return

    # Берём самый большой размер: витрина показывает фото на весь экран.
    file_name = f"{uuid4().hex}.jpg"
    await bot.download(message.photo[-1].file_id, destination=config.PHOTOS / file_name)
    photos.append(file_name)
    await state.update_data(photos=photos)

    if len(photos) == 1:
        await message.answer("Фото принято. Ещё? Или жми кнопку.", reply_markup=_done_keyboard())
    else:
        await message.answer(f"Фото принято ({len(photos)}).", reply_markup=_done_keyboard())


@router.message(NewProduct.photos, F.text)
async def add_photos_text(message: Message, state: FSMContext) -> None:
    if message.text.strip().lower() in CANCEL:
        await cancel(message, state)
        return
    await _ask_name(message, state)


@router.callback_query(NewProduct.photos, F.data == "add:photos_done")
async def add_photos_done(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _ask_name(callback.message, state)


async def _ask_name(message: Message, state: FSMContext) -> None:
    await state.set_state(NewProduct.name)
    await message.answer("Название вещи. Например: «Куртка Garment Dyed Crinkle Reps».")


@router.message(NewProduct.name, F.text)
async def add_name(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if text.lower() in CANCEL:
        await cancel(message, state)
        return
    await state.update_data(name=text[:120])
    await state.set_state(NewProduct.brand)
    await message.answer("Бренд. Если без бренда — «-».")


@router.message(NewProduct.brand, F.text)
async def add_brand(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if text.lower() in CANCEL:
        await cancel(message, state)
        return
    await state.update_data(brand="" if text.lower() in SKIP else text[:60])
    await state.set_state(NewProduct.category)
    await message.answer("Категория:", reply_markup=_categories_keyboard())


@router.callback_query(NewProduct.category, F.data.startswith("add:cat:"))
async def add_category(callback: CallbackQuery, state: FSMContext) -> None:
    category_id = int(callback.data.rsplit(":", 1)[1])
    await state.update_data(category_id=category_id or None)
    await state.set_state(NewProduct.price)
    await callback.answer()
    await callback.message.answer("Цена в рублях. Можно со скидкой: «24000 из 30000».")


@router.message(NewProduct.price, F.text)
async def add_price(message: Message, state: FSMContext) -> None:
    text = message.text.strip().lower()
    if text in CANCEL:
        await cancel(message, state)
        return

    numbers = [int(chunk) for chunk in "".join(
        character if character.isdigit() else " " for character in text.replace(" ", "")
    ).split()]
    if not numbers:
        await message.answer("Не понял цену. Напиши числом, например: 24000")
        return

    price = numbers[0]
    old_price = numbers[1] if len(numbers) > 1 and numbers[1] > price else None
    await state.update_data(price=price, old_price=old_price)
    await state.set_state(NewProduct.sizes)
    await message.answer(
        "Размеры и количество.\n"
        "Через пробел: «M L XL» — по одной штуке каждого.\n"
        "С количеством: «M:2 L:1».\n"
        "Вещь без размера в одном экземпляре — «-»."
    )


def parse_sizes(text: str) -> dict[str, int]:
    """Разбирает «M:2 L 32:3» в {'M': 2, 'L': 1, '32': 3}."""
    if text.strip().lower() in SKIP:
        return {db.ONE_SIZE: 1}
    sizes: dict[str, int] = {}
    for chunk in text.replace(",", " ").split():
        size, _, quantity = chunk.partition(":")
        size = size.strip().upper()
        if not size:
            continue
        sizes[size] = int(quantity) if quantity.strip().isdigit() else 1
    return sizes or {db.ONE_SIZE: 1}


@router.message(NewProduct.sizes, F.text)
async def add_sizes(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if text.lower() in CANCEL:
        await cancel(message, state)
        return
    await state.update_data(sizes=parse_sizes(text))
    await state.set_state(NewProduct.description)
    await message.answer("Описание: состояние, цвет, что важно знать. Или «-».")


@router.message(NewProduct.description, F.text)
async def add_description(message: Message, state: FSMContext, settings: config.Settings) -> None:
    text = message.text.strip()
    if text.lower() in CANCEL:
        await cancel(message, state)
        return

    data = await state.get_data()
    await state.clear()

    description = "" if text.lower() in SKIP else text[:1000]
    # Вещи с описанием «б/у», «носили», «состояние» помечаем как б/у автоматически:
    # владельцу не нужно отвечать на лишний вопрос ради поля, которое видно из текста.
    condition = "used" if any(
        word in description.lower() for word in ("б/у", "бу ", "носил", "секонд", "винтаж")
    ) else "new"

    product_id = db.add_product(
        name=data.get("name", "Без названия"),
        brand=data.get("brand", ""),
        category_id=data.get("category_id"),
        price=data.get("price", 0),
        old_price=data.get("old_price"),
        description=description,
        condition=condition,
        sizes=data.get("sizes") or {db.ONE_SIZE: 1},
        photos=data.get("photos") or [],
        source="admin",
    )

    product = db.get_product(product_id)
    await message.answer(
        f"Готово, товар №{product_id} в витрине.\n\n{_product_line(product, settings)}\n\n"
        "Добавить ещё — /add. Список товаров — /items.",
    )


def _product_line(product: dict, settings: config.Settings) -> str:
    sizes = ", ".join(
        f"{variant['size']}×{variant['available']}"
        for variant in product["sizes"]
        if variant["available"] > 0
    ) or "нет в наличии"
    label = {"active": "в витрине", "hidden": "скрыт", "sold": "продан"}[product["status"]]
    price = f"{product['price']:,}".replace(",", " ")
    title = f"{product['brand']} {product['name']}".strip()
    return f"№{product['id']} · {title}\n{price} {settings.currency} · {sizes} · {label}"


def _item_keyboard(product: dict) -> InlineKeyboardMarkup:
    hide_label = "Вернуть в витрину" if product["status"] != "active" else "Скрыть"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Остаток", callback_data=f"item:stock:{product['id']}"),
                InlineKeyboardButton(text=hide_label, callback_data=f"item:hide:{product['id']}"),
            ],
            [InlineKeyboardButton(text="Удалить", callback_data=f"item:delete:{product['id']}")],
        ]
    )


@router.message(Command("items"))
async def items(message: Message, settings: config.Settings) -> None:
    products = db.products_for_admin(limit=20)
    if not products:
        await message.answer("Товаров пока нет. Добавить — /add")
        return
    await message.answer(f"Последние {len(products)} товаров:")
    for product in products:
        await message.answer(_product_line(product, settings), reply_markup=_item_keyboard(product))


@router.callback_query(F.data.startswith("item:hide:"))
async def toggle_hidden(callback: CallbackQuery, settings: config.Settings) -> None:
    product_id = int(callback.data.rsplit(":", 1)[1])
    product = db.get_product(product_id)
    if not product:
        await callback.answer("Товар уже удалён", show_alert=True)
        return
    new_status = "active" if product["status"] != "active" else "hidden"
    db.update_product(product_id, status=new_status)
    updated = db.get_product(product_id)
    await callback.message.edit_text(_product_line(updated, settings), reply_markup=_item_keyboard(updated))
    await callback.answer("В витрине" if new_status == "active" else "Скрыт")


@router.callback_query(F.data.startswith("item:delete:"))
async def delete_item(callback: CallbackQuery) -> None:
    product_id = int(callback.data.rsplit(":", 1)[1])
    db.delete_product(product_id)
    await callback.message.edit_text(f"Товар №{product_id} удалён.")
    await callback.answer("Удалено")


@router.callback_query(F.data.startswith("item:stock:"))
async def ask_stock(callback: CallbackQuery, state: FSMContext) -> None:
    product_id = int(callback.data.rsplit(":", 1)[1])
    await state.set_state(EditStock.value)
    await state.update_data(product_id=product_id)
    await callback.answer()
    await callback.message.answer(
        f"Новые остатки для товара №{product_id}.\n"
        "Формат тот же: «M:2 L:1», «M L», «-» для вещи без размера.\n"
        "Размеры, которых нет в списке, обнулятся."
    )


@router.message(EditStock.value, F.text)
async def set_stock(message: Message, state: FSMContext, settings: config.Settings) -> None:
    text = message.text.strip()
    if text.lower() in CANCEL:
        await cancel(message, state)
        return

    data = await state.get_data()
    await state.clear()
    product_id = int(data["product_id"])
    product = db.get_product(product_id)
    if not product:
        await message.answer("Товар уже удалён.")
        return

    sizes = parse_sizes(text)
    # Обнуляем всё, чего нет в новом списке: «остатки» — это полная картина,
    # иначе исчезнувший размер навсегда останется висеть в витрине.
    for variant in product["sizes"]:
        if variant["size"] not in sizes:
            db.set_quantity(product_id, variant["size"], 0)
    for size, quantity in sizes.items():
        db.set_quantity(product_id, size, quantity)

    if product["status"] == "sold" and any(sizes.values()):
        db.update_product(product_id, status="active")

    await message.answer(_product_line(db.get_product(product_id), settings))
