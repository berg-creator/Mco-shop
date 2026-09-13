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

import asyncio
import logging
from html import escape
from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import BaseFilter, Command
from aiogram.filters.logic import or_f
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from . import config, db, music
from .bot import ADMIN_BUTTONS, BTN_ADD, BTN_CANCEL, BTN_ITEMS, BTN_MUSIC, BTN_SHOP

log = logging.getLogger(__name__)

MAX_PHOTOS = 5
# Больше двадцати мегабайт Bot API боту не отдаёт — это его предел на скачивание,
# и упереться в него молча значит оставить владельца гадать, что случилось.
MAX_TRACK = 20 * 1024 * 1024
# Ответ мастеру — это ответ на его вопрос, а не команда. Без этого условия
# `/start`, набранный посреди «Остатка», становился размером товара.
PLAIN_TEXT = F.text & ~F.text.startswith("/")
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


class NewMusic(StatesGroup):
    file = State()


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


@router.message(F.text.startswith("/") | F.text.in_(ADMIN_BUTTONS))
async def command_beats_wizard(message: Message, state: FSMContext) -> None:
    """Команда или кнопка обрывает мастер, а не становится ответом на его вопрос.

    Мастер ловит любой текст, и брошенный на полпути «Остаток» съедал
    следующий `/start`: размером товара становилось слово «/START», а до
    магазина команда не доходила. Теперь черновик выбрасывается, а событие
    идёт дальше — к обработчику самой команды.
    """
    await state.clear()
    raise SkipHandler


@router.message(or_f(Command("cancel"), F.text == BTN_CANCEL))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменил.")


@router.message(or_f(Command("add"), F.text == BTN_ADD))
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
    try:
        await bot.download(message.photo[-1].file_id, destination=config.PHOTOS / file_name)
    except Exception as error:  # noqa: BLE001 — связь до Telegram может подвести
        # Молчать нельзя: владелец решит, что фото принято, и товар уедет без него.
        log.warning("фото не скачалось: %s", error)
        await message.answer("Фото не скачалось. Пришли ещё раз или продолжай без него.")
        return
    photos.append(file_name)
    await state.update_data(photos=photos)

    if len(photos) == 1:
        await message.answer("Фото принято. Ещё? Или жми кнопку.", reply_markup=_done_keyboard())
    else:
        await message.answer(f"Фото принято ({len(photos)}).", reply_markup=_done_keyboard())


@router.message(NewProduct.photos, PLAIN_TEXT)
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


@router.message(NewProduct.name, PLAIN_TEXT)
async def add_name(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if text.lower() in CANCEL:
        await cancel(message, state)
        return
    await state.update_data(name=text[:120])
    await state.set_state(NewProduct.brand)
    await message.answer("Бренд. Если без бренда — «-».")


@router.message(NewProduct.brand, PLAIN_TEXT)
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


@router.message(NewProduct.price, PLAIN_TEXT)
async def add_price(message: Message, state: FSMContext) -> None:
    text = message.text.strip().lower()
    if text in CANCEL:
        await cancel(message, state)
        return

    numbers = [int(chunk) for chunk in "".join(
        character if character.isdigit() else " " for character in text.replace(" ", "")
    ).split()]
    # Ноль в цене — это не скидка, а вещь, которую заберут бесплатно.
    if not numbers or numbers[0] <= 0:
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
        # Размер — это метка на бирке, а не фраза: в витрине кнопка на пол-экрана
        # выглядит поломкой, поэтому длинное обрезаем.
        size = size.strip().upper()[:6]
        if not size:
            continue
        sizes[size] = int(quantity) if quantity.strip().isdigit() else 1
    return sizes or {db.ONE_SIZE: 1}


@router.message(NewProduct.sizes, PLAIN_TEXT)
async def add_sizes(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if text.lower() in CANCEL:
        await cancel(message, state)
        return
    await state.update_data(sizes=parse_sizes(text))
    await state.set_state(NewProduct.description)
    await message.answer("Описание: состояние, цвет, что важно знать. Или «-».")


@router.message(NewProduct.description, PLAIN_TEXT)
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
        f"Добавить ещё — «{BTN_ADD}», весь список — «{BTN_ITEMS}».",
    )


# --- музыка витрины ----------------------------------------------------------

@router.message(or_f(Command("music"), F.text == BTN_MUSIC))
async def music_start(message: Message, state: FSMContext) -> None:
    """Плейлист витрины: фото листаются в темпе той песни, что играет сейчас."""
    await state.clear()
    await state.set_state(NewMusic.file)
    await message.answer(
        f"{_playlist_lines()}\n\n"
        "Присылай песни файлами — витрина играет их подряд и каждый раз "
        "в новом порядке, а фото в карточке листаются в темпе того, что "
        "играет. Темп посчитаю сам.\n\n"
        "Убрать всю музыку — «-». Выйти: «отмена»."
    )


def _playlist_lines() -> str:
    """Что сейчас в плейлисте — списком, по строке на песню."""
    tracks = music.playlist()
    if not tracks:
        return "Сейчас в витрине тихо: музыки нет."
    songs = "\n".join(
        f"{number}. {escape(track.get('title') or track['file'])} — {track['bpm']} BPM"
        for number, track in enumerate(tracks, 1)
    )
    return f"Сейчас в плейлисте {len(tracks)}:\n{songs}"


# Песня добавляется без состояния мастера: аудиофайл от владельца — это
# всегда музыка для витрины, а альбом присылается пачкой, и требовать
# «Музыку» перед каждым файлом значило бы жать кнопку одиннадцать раз.
@router.message(F.audio | (F.document & F.document.mime_type.startswith("audio/")))
async def music_file(message: Message, state: FSMContext, bot: Bot) -> None:
    track = message.audio or message.document
    if (track.file_size or 0) > MAX_TRACK:
        await message.answer(
            f"Файл больше {MAX_TRACK // 1024 // 1024} МБ — столько Telegram боту не отдаёт. "
            "Пришли версию полегче."
        )
        return

    config.MUSIC.mkdir(parents=True, exist_ok=True)
    name = music.free_name(music.suffix_for(track.mime_type))
    # Имя занимается пустым файлом сразу: альбом приходит пачкой, обработчики
    # у aiogram работают параллельно, и без этого две песни, скачиваемые
    # одновременно, выбрали бы одно имя и одна затёрла бы другую.
    (config.MUSIC / name).touch()
    try:
        await bot.download(track.file_id, destination=config.MUSIC / name)
    except Exception as error:  # noqa: BLE001 — связь до Telegram может подвести
        log.warning("трек не скачался: %s", error)
        (config.MUSIC / name).unlink(missing_ok=True)
        await message.answer("Файл не скачался. Пришли ещё раз.")
        return

    await state.clear()
    # Счёт занимает секунды, но он считает, а не ждёт: в общем цикле это
    # заморозило бы бота вместе с заявками покупателей.
    info = await asyncio.to_thread(music.analyze, config.MUSIC / name)
    # Название — из тегов файла, а если их нет, из имени файла без расширения.
    title = getattr(track, "title", None) or (getattr(track, "file_name", "") or "").rsplit(".", 1)[0]
    count = music.add(name, title or "Без названия", info or music.DEFAULT_FLIP)
    if info:
        await message.answer(
            f"Взял, в плейлисте {count}.\n\n"
            f"Темп: {info['bpm']} BPM, снимок держится {info['flip_ms'] / 1000:.1f} с — "
            "фото листаются по долям.\n"
            f"Присылай ещё или открой витрину кнопкой «{BTN_SHOP}» и проверь."
        )
    else:
        await message.answer(
            f"Песню взял, в плейлисте {count}. Темп определить не вышло "
            "(на сервере нет ffmpeg или это не музыка) — под неё фото листаются раз в 3 секунды."
        )


@router.message(NewMusic.file, PLAIN_TEXT)
async def music_text(message: Message, state: FSMContext) -> None:
    text = message.text.strip().lower()
    if text in CANCEL:
        await cancel(message, state)
        return
    if text in SKIP:
        await state.clear()
        music.forget()
        await message.answer("Убрал музыку, в витрине тихо.")
        return
    await message.answer("Жду песню файлом. Или «-» — убрать музыку совсем.")


def _product_line(product: dict, settings: config.Settings) -> str:
    """Строка товара для админки. Название экранируется: бот пишет в HTML,
    а в названиях из канала попадаются угловые скобки — с ними Telegram
    отказался бы отправить сообщение, и /items выглядел бы сломанным."""
    sizes = ", ".join(
        f"{escape(variant['size'])}×{variant['available']}"
        for variant in product["sizes"]
        if variant["available"] > 0
    ) or "нет в наличии"
    label = {"active": "в витрине", "hidden": "скрыт", "sold": "продан"}.get(
        product["status"], product["status"]
    )
    price = f"{product['price']:,}".replace(",", " ")
    title = escape(f"{product['brand']} {product['name']}".strip())
    return f"№{product['id']} · {title}\n{price} {settings.currency} · {sizes} · {label}"


def _item_keyboard(product: dict) -> InlineKeyboardMarkup:
    """Кнопки под товаром зависят от того, есть ли он в наличии.

    У распроданной вещи прятать нечего, а «вернуть в витрину» ей не поможет:
    статус сменится, но с нулевым остатком покупатель её всё равно не увидит.
    Поэтому там одна кнопка — вернуть остаток.
    """
    available = sum(variant["available"] for variant in product["sizes"])
    product_id = product["id"]

    if available:
        toggle = "Скрыть" if product["status"] == "active" else "Вернуть в витрину"
        first_row = [
            InlineKeyboardButton(text="Остаток", callback_data=f"item:stock:{product_id}"),
            InlineKeyboardButton(text=toggle, callback_data=f"item:hide:{product_id}"),
        ]
    else:
        first_row = [
            InlineKeyboardButton(text="Вернуть остаток", callback_data=f"item:stock:{product_id}")
        ]

    return InlineKeyboardMarkup(
        inline_keyboard=[
            first_row,
            [InlineKeyboardButton(text="Удалить", callback_data=f"item:delete:{product_id}")],
        ]
    )


@router.message(or_f(Command("items"), F.text == BTN_ITEMS))
async def items(message: Message, settings: config.Settings) -> None:
    """Список товаров, а с запросом — поиск по нему.

    Без запроса — последние двадцать. С запросом — номер товара или кусок
    названия: в витрине под сотню вещей, и до старой карточки с телефона
    иначе не добраться. Отбор в Python, а не в SQL: LIKE в SQLite не знает
    регистра кириллицы, и «куртка» не нашла бы «Куртка».
    """
    # Запрос бывает только у команды: у кнопки в тексте пробел тоже есть,
    # и без этой проверки «📦 Товары» искалось бы как слово «товары».
    query = message.text.partition(" ")[2].strip().lower() if message.text.startswith("/") else ""
    if query.isdigit():
        product = db.get_product(int(query))
        products = [product] if product else []
    elif query:
        products = [
            product for product in db.products_for_admin(limit=500)
            if query in f"{product['brand']} {product['name']}".lower()
        ][:20]
    else:
        products = db.products_for_admin(limit=20)

    if not products:
        await message.answer(
            f"Ничего не нашлось. Весь список — «{BTN_ITEMS}»" if query
            else f"Товаров пока нет. Заводится кнопкой «{BTN_ADD}»"
        )
        return
    await message.answer(
        f"Нашёл {len(products)}:" if query else f"Последние {len(products)} товаров:"
    )
    for product in products:
        await message.answer(_product_line(product, settings), reply_markup=_item_keyboard(product))


@router.callback_query(F.data.startswith("item:hide:"))
async def toggle_hidden(callback: CallbackQuery, settings: config.Settings) -> None:
    product_id = int(callback.data.rsplit(":", 1)[1])
    product = db.get_product(product_id)
    if not product:
        await callback.answer("Товар уже удалён", show_alert=True)
        return

    available = sum(variant["available"] for variant in product["sizes"])
    if product["status"] != "active" and not available:
        # Иначе получится товар «в витрине», которого никто не видит:
        # витрина показывает только то, что есть в наличии.
        await callback.answer("Вещь распродана — сначала верни остаток", show_alert=True)
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


@router.message(EditStock.value, PLAIN_TEXT)
async def set_stock(message: Message, state: FSMContext, settings: config.Settings) -> None:
    text = message.text.strip()
    if text.lower() in CANCEL:
        await cancel(message, state)
        return

    data = await state.get_data()
    await state.clear()
    product_id = int(data.get("product_id") or 0)
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
