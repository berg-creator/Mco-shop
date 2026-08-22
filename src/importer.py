"""Разовый перенос каталога из Telegram-канала в витрину.

Товары магазина уже описаны — постами в канале, руками, живым языком.
Вбивать их заново в админку значит потратить вечер на то, что можно разобрать
машинно. Экспорт из Telegram Desktop (Настройки → Экспорт данных → формат JSON,
вместе с фото) даёт `result.json`, где лежат все посты и пути к картинкам.

**Разбор эвристический и это признаётся честно.** Пост не заполняет форму:
цена может быть «12к», размер — «M/L», а половина постов вообще не про товар.
Поэтому импорт двухшаговый: сначала черновик JSON, который человек глазами
правит и вычёркивает лишнее, потом заливка в базу. Молча заливать угаданное
в витрину нельзя — там будут цены, по которым придётся продавать.

    python -m src.importer ~/export/result.json --dry-run      посмотреть разбор
    python -m src.importer ~/export/result.json --out draft.json   черновик
    python -m src.importer --apply draft.json                   залить в базу
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import config, db
from .scrape import BROWSER

# Бренды ниши: помогают отделить название вещи от марки. Список пополняется —
# он не претендует на полноту, всё неузнанное просто остаётся без бренда.
KNOWN_BRANDS = [
    "Stone Island", "C.P. Company", "CP Company", "Ma.Strum", "Ten C", "Nemen",
    "Arc'teryx", "Arcteryx", "Salomon", "The North Face", "Patagonia", "Berghaus",
    "Carhartt WIP", "Carhartt", "Barbour", "Belstaff", "Moncler", "Napapijri",
    "Maison Margiela", "Margiela", "Raf Simons", "Helmut Lang", "Yohji Yamamoto",
    "Comme des Garcons", "Comme des Garçons", "Rick Owens", "Acronym", "Vetements",
    "Balenciaga", "Prada", "Miu Miu", "Undercover", "Junya Watanabe",
    "Supreme", "Palace", "Stussy", "Stüssy", "Nike", "Adidas", "New Balance",
    "Asics", "Vans", "Converse", "Reebok", "Levi's", "Levis", "Porter Yoshida",
    "Porter", "Eastpak", "Gramicci", "Snow Peak", "And Wander", "Goldwin",
]

# Ключевые слова категорий — и русские, и английские: в постах про одежду
# половина названий на латинице («double knee», «cargo pants»), и без них
# треть каталога уезжает в «без категории».
CATEGORY_WORDS = {
    "jackets": ["куртк", "анорак", "парк", "ветровк", "пуховик", "бомбер", "жилет", "плащ",
                 "пальто", "jacket", "parka", "anorak", "coat", "vest", "overshirt", "shell"],
    "pants": ["штан", "брюк", "карго", "джинс", "шорт", "трек",
               "pant", "trouser", "jean", "cargo", "short", "denim", "knee"],
    "hoodies": ["худи", "толстовк", "свитшот", "кофт", "свитер", "флис", "зип",
                 "hoodie", "sweatshirt", "crewneck", "fleece", "sweater", "knit"],
    "shirts": ["футболк", "рубашк", "поло", "лонгслив", "майк",
                "tee", "t-shirt", "shirt", "polo", "longsleeve"],
    "shoes": ["кроссовк", "ботинк", "обувь", "сникер", "кед", "лофер", "сандал",
               "sneaker", "boot", "shoe", "trainer"],
    "accessories": ["шапк", "кепк", "панам", "сумк", "ремен", "перчатк", "шарф",
                     "рюкзак", "носки", "барсетк", "балаклав", "очки",
                     "cap", "hat", "beanie", "bag", "belt", "scarf", "glove", "backpack", "sock"],
}

SIZE_WORDS = re.compile(
    r"\b(XXS|XS|S|M|L|XL|XXL|XXXL|2XL|3XL|"
    r"[2-5][02468]|3[6-9]|4[0-9])\b",
    re.IGNORECASE,
)
SOLD_WORDS = ("продано", "sold", "продан", "забрали", "ушла", "ушло")

# «42 000 ₽», «12000 руб», «12к», «цена 8500»
PRICE_PATTERNS = [
    re.compile(r"(\d[\d\s]{2,})\s*(?:₽|руб|р\.|rub)", re.IGNORECASE),
    re.compile(r"(?:цена|price|стоимость)\D{0,3}(\d[\d\s]{2,})", re.IGNORECASE),
    re.compile(r"\b(\d{1,3})\s*[кk]\b", re.IGNORECASE),
]


def plain_text(raw: Any) -> str:
    """Экспорт хранит текст либо строкой, либо списком кусков с разметкой."""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts = []
        for piece in raw:
            if isinstance(piece, str):
                parts.append(piece)
            elif isinstance(piece, dict):
                parts.append(str(piece.get("text", "")))
        return "".join(parts)
    return ""


def find_price(text: str) -> int | None:
    for index, pattern in enumerate(PRICE_PATTERNS):
        match = pattern.search(text)
        if not match:
            continue
        number = int(re.sub(r"\D", "", match.group(1)))
        if index == 2:  # «12к» — это двенадцать тысяч
            number *= 1000
        # Отсекаем годы и артикулы: одежда дешевле 300 ₽ и дороже 3 млн не бывает.
        if 300 <= number <= 3_000_000:
            return number
    return None


def find_brand(text: str) -> str:
    lowered = text.lower()
    # Длинные названия проверяем раньше коротких: «Carhartt WIP» важнее «Carhartt».
    for brand in sorted(KNOWN_BRANDS, key=len, reverse=True):
        if brand.lower() in lowered:
            return brand
    return ""


def find_category(text: str) -> str:
    lowered = text.lower()
    for slug, words in CATEGORY_WORDS.items():
        if any(word in lowered for word in words):
            return slug
    return ""


def find_sizes(text: str) -> dict[str, int]:
    """Ищет размеры в строке вида «размер M» или «S/M/L», «44-46»."""
    match = re.search(r"(?:размер|size|рост)\w*\s*[:\-—]?\s*([^\n.;]+)", text, re.IGNORECASE)
    scope = match.group(1) if match else ""
    found = [size.upper() for size in SIZE_WORDS.findall(scope)]
    # Без явного слова «размер» ищем по всему тексту только буквенные размеры:
    # числа в описании чаще оказываются годом или ростом, чем размером.
    if not found:
        found = [size.upper() for size in re.findall(r"\b(XXS|XS|S|M|L|XL|XXL)\b", text)]
    unique = list(dict.fromkeys(found))
    return {size: 1 for size in unique}


def guess_name(text: str, brand: str) -> str:
    """Название — первая содержательная строка без бренда и цены."""
    for line in text.splitlines():
        cleaned = line.strip(" •-—*·#\t")
        if len(cleaned) < 3:
            continue
        if brand:
            cleaned = re.sub(re.escape(brand), "", cleaned, flags=re.IGNORECASE).strip(" -—·|,")
        cleaned = re.sub(r"\s*\d[\d\s]{2,}\s*(?:₽|руб|р\.)?\s*$", "", cleaned).strip()
        if len(cleaned) >= 3:
            return cleaned[:120]
    return "Без названия"


def parse_export(path: Path) -> list[dict[str, Any]]:
    """Превращает выгрузку канала в черновики карточек.

    Альбом в экспорте — это несколько сообщений подряд: текст у первого,
    у остальных только фото. Поэтому фотографии без текста прикрепляются
    к предыдущему разобранному товару.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    messages = data.get("messages", []) if isinstance(data, dict) else (
        data if isinstance(data, list) else []
    )
    export_dir = path.parent

    drafts: list[dict[str, Any]] = []
    for message in messages:
        if message.get("type") != "message":
            continue

        text = plain_text(message.get("text", "")).strip()
        photo = message.get("photo") or ""
        photo_path = str(export_dir / photo) if photo else ""

        if not text and photo_path and drafts:
            drafts[-1]["photos"].append(photo_path)
            continue
        if not text:
            continue

        price = find_price(text)
        if price is None:
            # Пост без цены — это анонс, отзыв или «доброе утро», а не товар.
            continue

        brand = find_brand(text)
        drafts.append(
            {
                "post_id": message.get("id"),
                "date": message.get("date", ""),
                "name": guess_name(text, brand),
                "brand": brand,
                "category": find_category(text),
                "price": price,
                "sizes": find_sizes(text) or {db.ONE_SIZE: 1},
                "description": text,
                "photos": [photo_path] if photo_path else [],
                "sold": any(word in text.lower() for word in SOLD_WORDS),
            }
        )
    return drafts


def save_photo(source: str) -> str | None:
    """Кладёт фотографию в `data/photos` — из файла экспорта или по ссылке.

    Сборщик из веб-превью (`src/scrape.py`) даёт ссылки на картинки Telegram,
    экспорт — пути к файлам на диске. Дальше витрине всё равно, откуда фото
    приехало: она отдаёт их из своей папки.
    """
    target_name = f"{uuid4().hex}.jpg"
    target = config.PHOTOS / target_name

    if source.startswith(("http://", "https://")):
        request = urllib.request.Request(source, headers={"User-Agent": BROWSER})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                target.write_bytes(response.read())
        except (urllib.error.URLError, TimeoutError) as error:
            print(f"  фото не скачалось ({error}): {source[:60]}…")
            return None
        return target_name

    source_path = Path(source)
    if not source_path.exists():
        return None
    shutil.copyfile(source_path, target)
    return target_name


def apply_drafts(drafts: list[dict[str, Any]], skip_sold: bool = True) -> int:
    """Заливает черновики в базу, забирая к себе фотографии."""
    db.init()
    slugs = {category["slug"]: category["id"] for category in db.categories()}
    config.PHOTOS.mkdir(parents=True, exist_ok=True)

    added = 0
    for draft in drafts:
        if skip_sold and draft.get("sold"):
            continue

        # Черновик правит человек в текстовом редакторе, и «12 000» вместо 12000
        # там появляется легко. Один такой черновик не должен обрывать импорт
        # на середине: остальные заливаются, а про этот сказано вслух.
        try:
            price = int(draft.get("price", 0))
            old_price = int(draft["old_price"]) if draft.get("old_price") else None
            sizes = {
                str(size): int(count)
                for size, count in (draft.get("sizes") or {db.ONE_SIZE: 1}).items()
            }
        except (TypeError, ValueError) as error:
            print(f"  черновик №{draft.get('post_id', '?')} пропущен: {error}")
            continue

        photos = [name for name in (save_photo(source) for source in draft.get("photos", [])) if name]

        description = draft.get("description", "")
        condition = draft.get("condition") or (
            "used" if any(word in description.lower() for word in ("б/у", "носил", "секонд", "винтаж"))
            else "new"
        )
        db.add_product(
            name=draft.get("name", "Без названия"),
            brand=draft.get("brand", ""),
            category_id=slugs.get(draft.get("category", "")),
            price=price,
            old_price=old_price,
            description=description,
            condition=condition,
            sizes=sizes,
            photos=photos,
            source="channel",
            source_ref=str(draft.get("post_id", "")),
        )
        added += 1
    return added


def _print_report(drafts: list[dict[str, Any]]) -> None:
    sold = sum(1 for draft in drafts if draft["sold"])
    no_category = sum(1 for draft in drafts if not draft["category"])
    no_brand = sum(1 for draft in drafts if not draft["brand"])
    print(f"разобрано постов с ценой: {len(drafts)}")
    print(f"  помечены проданными:    {sold}")
    print(f"  без категории:          {no_category}")
    print(f"  без бренда:             {no_brand}")
    print("\nпервые пять карточек:\n")
    for draft in drafts[:5]:
        sizes = ", ".join(draft["sizes"]) or "—"
        print(f"  №{draft['post_id']} · {draft['brand'] or '—'} · {draft['name']}")
        print(f"     {draft['price']} ₽ · {draft['category'] or 'без категории'} · размеры: {sizes}"
              f" · фото: {len(draft['photos'])}{' · ПРОДАНО' if draft['sold'] else ''}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Импорт каталога из экспорта канала")
    parser.add_argument("export", nargs="?", help="путь к result.json из Telegram Desktop")
    parser.add_argument("--dry-run", action="store_true", help="показать разбор, ничего не писать")
    parser.add_argument("--out", help="сохранить черновик в JSON для правки руками")
    parser.add_argument("--apply", help="залить в базу готовый черновик")
    parser.add_argument("--with-sold", action="store_true", help="заливать и проданное (скрытым)")
    args = parser.parse_args(argv)

    if args.apply:
        drafts = json.loads(Path(args.apply).read_text(encoding="utf-8"))
        added = apply_drafts(drafts, skip_sold=not args.with_sold)
        print(f"добавлено товаров: {added}")
        return 0

    if not args.export:
        parser.print_help()
        return 1

    export_path = Path(args.export).expanduser()
    if not export_path.exists():
        print(f"файла нет: {export_path}")
        return 1

    drafts = parse_export(export_path)
    _print_report(drafts)

    if args.out:
        out_path = Path(args.out).expanduser()
        out_path.write_text(json.dumps(drafts, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nчерновик записан: {out_path}")
        print("Поправь его руками (цены, названия, категории), потом:")
        print(f"    python -m src.importer --apply {out_path}")
    elif not args.dry_run:
        print("\nЧто дальше: --out draft.json — сохранить черновик, --dry-run — только посмотреть")
    return 0


if __name__ == "__main__":
    sys.exit(main())
