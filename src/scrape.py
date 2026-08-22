"""Сбор каталога прямо из публичного Telegram-канала.

Экспорт из Telegram Desktop есть не всегда и не у всех, а публичный канал
отдаёт свои посты веб-версией `t.me/s/<канал>` — обычной HTML-страницей.
Этого достаточно, чтобы поднять витрину из уже написанных постов, не заводя
каталог заново руками.

Разбор устроен под формат карточки этого магазина, а не «под любой канал»:

    STONE ISLAND HOODIE
    Размер: L
    Состояние: NEW, с навесными бирками
    Стоимость: 17.990₽
    Купить: @sfmmfu

Наличие живёт не в самом посте, а в ответах на него: «❗️42 ПРОДАНО❗️» убирает
одну пару этого размера, «❗️ПРОДАНО❗️» закрывает вещь целиком, а «8.000₽ —
новая цена» меняет цену и переносит старую в зачёркнутую. Поэтому посты
разбираются вместе с ответами и в порядке возрастания номера: последнее
обновление — самое верное.

Что этот способ не умеет и лечится только руками: цену «в личку», подборки
«можем привезти под заказ» (они пропускаются), количество из фразы «привезли
ещё пару размеров» — такие посты попадают в отчёт отдельным списком.

Разбор HTML сделан регулярными выражениями: разметка веб-превью простая
и стабильная, а тянуть в проект парсер ради одного разового сбора незачем.

    python -m src.scrape Mcoworldwide --dry-run       посмотреть, что выйдет
    python -m src.scrape Mcoworldwide --out draft.json  черновик на правку
    python -m src.importer --apply draft.json           залить в базу
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BROWSER = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"

# Разметка веб-превью канала
POST_SPLIT = re.compile(r'(?=<div class="tgme_widget_message [^"]*" data-post=)')
POST_ID = re.compile(r'data-post="[^/]+/(\d+)"')
REPLY_TO = re.compile(r'<a class="tgme_widget_message_reply[^"]*"\s*href="[^"]*?/(\d+)"')
# js-message_text — сам пост; у цитаты класс js-message_reply_text, и путать их нельзя
MESSAGE_TEXT = re.compile(r'<div class="tgme_widget_message_text js-message_text"[^>]*>(.*?)</div>', re.S)
PHOTO_URL = re.compile(r"tgme_widget_message_photo_wrap[^>]*background-image:url\('([^']+)'\)")
POST_TIME = re.compile(r'<time datetime="([^"]+)"')
MORE_BEFORE = re.compile(r'data-before="(\d+)"')

# «13.990₽», «12 000 ₽», «8.000₽» — в канале разделитель тысяч точка
MONEY = re.compile(r"(\d{1,3}(?:[.\s]\d{3})+|\d{3,7})\s*₽")
SIZE_LINE = re.compile(r"^\s*размер\w*\s*(?:\([^)]*\))?\s*:?\s*(.+)$", re.IGNORECASE)
CONDITION_LINE = re.compile(r"^\s*состояние\s*:?\s*(.+)$", re.IGNORECASE)
PRICE_LINE = re.compile(r"^\s*(?:стоимость|цена)\s*:?\s*(.+)$", re.IGNORECASE)
SOLD_SIZE = re.compile(r"([A-ZА-Я0-9][A-ZА-Я0-9.]*)\s+продано", re.IGNORECASE)

CATEGORY_WORDS = {
    "shoes": ["converse", "sneaker", "shox", "кроссов", "boot", "drkstar", "turbodrk"],
    "jackets": ["jacket", "bomber", "parka", "shell", "coat", "куртк", "анорак", "gilet", "vest"],
    "hoodies": ["hoodie", "sweatshirt", "sweater", "jumper", "fleece", "cardigan",
                 "turtle neck", "knit", "crewneck", "худи", "свитер"],
    "shirts": ["polo", "t-shirt", "tee", "shirt", "футболк", "рубашк"],
    "pants": ["pants", "jeans", "trousers", "shorts", "штан", "джинс", "брюк"],
    "accessories": ["bag", "cap", "hat", "beanie", "belt", "сумк", "шапк", "ремен", "кепк"],
}

# Бренды, которые встречаются в этом канале. Список нужен, чтобы отделить марку
# от модели: «STONE ISLAND TEDDY FLEECE» → бренд «Stone Island», вещь «Teddy Fleece».
BRANDS = [
    "Rick Owens DRKSHDW", "Rick Owens", "Stone Island", "Polo Ralph Lauren", "Ralph Lauren",
    "Alpha Industries", "Karl Lagerfeld", "Palm Angels", "Carhartt WIP", "Carhartt",
    "Nogletcher", "BAPE", "A Bathing Ape", "Nike", "Converse", "Adidas", "The North Face",
    "C.P. Company", "Napapijri", "Arc'teryx", "Supreme", "Off-White", "Represent",
]


def fetch(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": BROWSER})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def clean_text(fragment: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", fragment)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def parse_page(page: str) -> list[dict[str, Any]]:
    messages = []
    for chunk in POST_SPLIT.split(page):
        found = POST_ID.search(chunk)
        if not found:
            continue
        texts = MESSAGE_TEXT.findall(chunk)
        reply = REPLY_TO.search(chunk)
        moment = POST_TIME.search(chunk)
        messages.append(
            {
                "id": int(found.group(1)),
                "reply_to": int(reply.group(1)) if reply else None,
                "text": clean_text(texts[0]) if texts else "",
                "photos": PHOTO_URL.findall(chunk),
                "date": moment.group(1) if moment else "",
            }
        )
    return messages


def collect_messages(channel: str, max_pages: int = 40, pause: float = 1.0) -> list[dict[str, Any]]:
    """Идёт по архиву канала от свежих постов к старым.

    Пауза между страницами намеренная: это чужой сервер, и вежливость дешевле
    временной блокировки посреди сбора.
    """
    messages: dict[int, dict[str, Any]] = {}
    before = ""
    for page_number in range(max_pages):
        url = f"https://t.me/s/{channel}" + (f"?before={before}" if before else "")
        try:
            page = fetch(url)
        except urllib.error.HTTPError as error:
            print(f"страница {page_number + 1}: сервер ответил {error.code}, останавливаюсь")
            break
        except urllib.error.URLError as error:
            print(f"страница {page_number + 1}: нет связи ({error.reason})")
            break

        batch = parse_page(page)
        if not batch:
            break
        for message in batch:
            messages[message["id"]] = message

        more = MORE_BEFORE.search(page)
        print(f"  страница {page_number + 1}: постов {len(batch)}, всего {len(messages)}")
        if not more:
            break
        before = more.group(1)
        time.sleep(pause)
    return [messages[key] for key in sorted(messages)]


def parse_money(text: str) -> int | None:
    match = MONEY.search(text)
    if not match:
        return None
    return int(re.sub(r"\D", "", match.group(1)))


def find_brand(name: str) -> str:
    lowered = name.lower()
    for brand in sorted(BRANDS, key=len, reverse=True):
        if brand.lower() in lowered:
            return brand
    return ""


def strip_brand(name: str, brand: str) -> str:
    """Убирает марку из названия: в витрине бренд стоит отдельной строкой.

    «STONE ISLAND TEDDY FLEECE» → «TEDDY FLEECE». Если после вычитания ничего
    не остаётся («NOGLETCHER» — название и есть марка), название сохраняется.
    """
    if not brand:
        return name
    cleaned = re.sub(re.escape(brand), "", name, flags=re.IGNORECASE)
    # Марка бывает в середине («VINTAGE CARHARTT ARCTIC JACKET»), после выреза
    # остаётся двойной пробел.
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" -—·|,")
    return cleaned or name


def find_category(text: str, shoe_hint: bool = False) -> str:
    if shoe_hint:
        return "shoes"
    lowered = text.lower()
    for slug, words in CATEGORY_WORDS.items():
        if any(word in lowered for word in words):
            return slug
    return ""


def parse_sizes(line: str) -> dict[str, int]:
    """«41, 41, 42» → {'41': 2, '42': 1}. Повтор размера в посте — это две вещи."""
    sizes: dict[str, int] = {}
    for piece in re.split(r"[,/]| и ", line):
        # «XS(факт S)» — берём то, что написано на бирке, примечание останется в описании
        piece = re.sub(r"\(.*?\)", "", piece).strip().upper()
        # «42 EUR», «EU 41», «размер 34» — единица измерения написана рядом
        # с числом и к бирке отношения не имеет. Без этого последний размер
        # в строке «41, 41, 42 EUR» молча терялся вместе с парой обуви.
        piece = re.sub(r"\b(EUR|EU|US|UK|RU|SIZE|РАЗМЕР|СМ|CM)\b", "", piece).strip()
        piece = piece.strip(".;:")
        if not piece or len(piece) > 6:
            continue
        if not re.fullmatch(r"[A-ZА-Я0-9]{1,3}(?:\.\d)?", piece):
            continue
        sizes[piece] = sizes.get(piece, 0) + 1
    return sizes


def clean_description(lines: list[str]) -> str:
    """Оставляет от поста только то, чего нет в полях карточки.

    Название, размер и цена витрина показывает сама, хештег канала здесь не
    к месту, а «Купить: @sfmmfu» уводит покупателя из бота мимо корзины —
    и заявка до владельца не доходит. Остаётся описание состояния и всё живое,
    что он написал про вещь.
    """
    kept = []
    for line in lines[1:]:
        if SIZE_LINE.match(line) or PRICE_LINE.match(line):
            continue
        if re.match(r"^\s*(купить|заказать|писать|связь|контакт)\s*:", line, re.IGNORECASE):
            continue
        cleaned = re.sub(r"#\S+", "", line)
        cleaned = re.sub(r"@\w+", "", cleaned).strip(" -—·|,")
        if cleaned:
            kept.append(cleaned)
    return "\n".join(kept)


def parse_product(message: dict[str, Any]) -> dict[str, Any] | None:
    """Разбирает пост-карточку. Возвращает None, если это не товар."""
    lines = [line.strip() for line in message["text"].split("\n") if line.strip()]
    if not lines:
        return None

    name = lines[0]
    size_text = condition = ""
    price = None
    for line in lines[1:]:
        if (found := SIZE_LINE.match(line)) and not size_text:
            size_text = found.group(1)
        elif (found := CONDITION_LINE.match(line)) and not condition:
            condition = found.group(1)
        elif (found := PRICE_LINE.match(line)) and price is None:
            price = parse_money(found.group(1))

    # Карточка товара — это цена вместе с размером. Пост «можем привезти такие
    # сумки, стоимость 12.000₽» — предложение под заказ, а не наличие.
    if price is None or not size_text:
        return None

    shoe_hint = "eur" in message["text"].lower()
    sizes = parse_sizes(size_text)
    brand = find_brand(name)
    return {
        "post_id": message["id"],
        "link": f"https://t.me/Mcoworldwide/{message['id']}",
        "date": message["date"],
        "name": strip_brand(name, brand)[:120],
        "brand": brand,
        "category": find_category(name, shoe_hint),
        "price": price,
        "old_price": None,
        "sizes": sizes,
        "condition": "used" if "б/у" in condition.lower() else "new",
        "description": clean_description(lines),
        "photos": message["photos"],
        "sold": "продано" in message["text"].lower(),
        "notes": [],
    }


def apply_updates(products: dict[int, dict[str, Any]], messages: list[dict[str, Any]]) -> None:
    """Накатывает ответы-обновления на карточки: продажи и смену цены."""
    for message in messages:
        parent = message["reply_to"]
        if parent is None or parent not in products:
            continue
        product = products[parent]
        text = message["text"]
        upper = text.upper()

        if "ПРОДАНО" in upper:
            sizes_sold = [
                size for size in SOLD_SIZE.findall(upper)
                if size not in {"ПРОДАНО"}
            ]
            if sizes_sold:
                for size in sizes_sold:
                    key = size.strip(".")
                    if key in product["sizes"]:
                        product["sizes"][key] -= 1
                        if product["sizes"][key] <= 0:
                            del product["sizes"][key]
                    else:
                        product["notes"].append(f"продан размер {key}, которого нет в посте")
            else:
                product["sold"] = True
            continue

        if "новая цена" in text.lower():
            # В посте про скидку упомянуты обе цены: старая зачёркнута, новая ниже.
            # Пост-карточку владелец к этому моменту мог уже отредактировать,
            # поэтому опираемся на числа из объявления, а не на разницу с карточкой.
            mentioned = sorted(int(re.sub(r"\D", "", candidate)) for candidate in MONEY.findall(text))
            if len(mentioned) >= 2 and mentioned[0] < mentioned[-1]:
                product["price"] = mentioned[0]
                product["old_price"] = mentioned[-1]
            elif mentioned:
                product["price"] = mentioned[0]
            continue

        # Всё остальное — живой комментарий владельца: «привезли ещё пару размеров».
        # Машина не знает, сколько именно привезли, поэтому это работа человеку.
        product["notes"].append(text[:200])


def build_drafts(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    products: dict[int, dict[str, Any]] = {}
    skipped: list[dict[str, Any]] = []
    for message in messages:
        if message["reply_to"] is not None:
            continue
        product = parse_product(message)
        if product:
            products[message["id"]] = product
        elif message["text"]:
            skipped.append(message)

    apply_updates(products, messages)

    drafts = []
    for product in products.values():
        if not product["sizes"]:
            product["sold"] = True
        drafts.append(product)
    return drafts, skipped


def _report(drafts: list[dict[str, Any]], skipped: list[dict[str, Any]]) -> None:
    live = [draft for draft in drafts if not draft["sold"]]
    print(f"\nвсего карточек:        {len(drafts)}")
    print(f"  в наличии:           {len(live)}")
    print(f"  продано:             {len(drafts) - len(live)}")
    print(f"  без категории:       {sum(1 for d in live if not d['category'])}")
    print(f"  без бренда:          {sum(1 for d in live if not d['brand'])}")
    print(f"  с примечаниями:      {sum(1 for d in live if d['notes'])}")
    print(f"пропущено постов:      {len(skipped)} (без цены или без размера)")

    print("\nв наличии:")
    for draft in live:
        sizes = ", ".join(f"{size}×{count}" for size, count in draft["sizes"].items())
        price = f"{draft['price']:,}".replace(",", " ")
        old = f" (было {draft['old_price']:,})".replace(",", " ") if draft["old_price"] else ""
        print(f"  №{draft['post_id']:<4} {draft['brand'] or '—':<18} {draft['name'][:44]:<44} "
              f"{price:>7} ₽{old} · {draft['category'] or 'без категории':<14} · {sizes} · фото {len(draft['photos'])}")
        for note in draft["notes"]:
            print(f"         ↳ примечание из канала: {note[:90]}")

    if skipped:
        print("\nпропущенные посты (проверь глазами):")
        for message in skipped:
            first = message["text"].split("\n")[0]
            print(f"  №{message['id']:<4} {first[:90]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Сбор каталога из публичного канала")
    parser.add_argument("channel", help="юзернейм канала без @, например Mcoworldwide")
    parser.add_argument("--out", help="сохранить черновик в JSON")
    parser.add_argument("--dry-run", action="store_true", help="только показать разбор")
    parser.add_argument("--pages", type=int, default=40, help="сколько страниц архива пройти")
    args = parser.parse_args(argv)

    print(f"собираю t.me/s/{args.channel}")
    messages = collect_messages(args.channel, max_pages=args.pages)
    if not messages:
        print("канал ничего не отдал: закрытый, переименован или Telegram недоступен")
        return 1

    drafts, skipped = build_drafts(messages)
    _report(drafts, skipped)

    if args.out:
        path = Path(args.out).expanduser()
        path.write_text(json.dumps(drafts, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nчерновик записан: {path}")
        print("Поправь названия, категории и остатки, потом:")
        print(f"    python -m src.importer --apply {path}")
    elif not args.dry_run:
        print("\nЧто дальше: --out draft.json — сохранить черновик для правки")
    return 0


if __name__ == "__main__":
    sys.exit(main())
