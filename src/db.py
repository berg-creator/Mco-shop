"""Хранилище магазина: SQLite-файл `data/shop.db`.

Почему база, а не JSON-файлы, как у канала-соседа: там состояние пишет один
процесс по расписанию, здесь одновременно пишут покупатели (заказы) и владелец
(остатки из админки). Нужны транзакции — иначе последнюю куртку продадут дважды.

Ключевое решение — **резерв вместо списания**. Заказ в этом магазине не оплата,
а заявка: пока владелец не подтвердил, товар не продан, но и предлагать его
второму покупателю нельзя. Поэтому у размера два числа: `quantity` (сколько есть)
и `reserved` (сколько ждёт подтверждения). Витрина показывает разницу.
Подтверждение заявки списывает остаток, отказ — снимает резерв.

Соединение открывается на каждую операцию и сразу закрывается: файл лежит рядом,
это дешевле, чем городить пул и следить за тем, из какого потока пришёл вызов.

    python -m src.db --init        создать пустую базу
    python -m src.db --demo        налить демо-товары, чтобы посмотреть витрину
    python -m src.db --check       что лежит в базе сейчас
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    id       INTEGER PRIMARY KEY,
    slug     TEXT NOT NULL UNIQUE,
    name     TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 100
);

CREATE TABLE IF NOT EXISTS products (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    brand       TEXT NOT NULL DEFAULT '',
    category_id INTEGER REFERENCES categories(id),
    price       INTEGER NOT NULL,              -- целые рубли: float на деньгах врёт
    old_price   INTEGER,                       -- зачёркнутая цена, если скидка
    description TEXT NOT NULL DEFAULT '',
    condition   TEXT NOT NULL DEFAULT 'new',   -- new | used
    color       TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'active',-- active | hidden | sold
    source      TEXT NOT NULL DEFAULT 'admin', -- admin | channel | import
    source_ref  TEXT NOT NULL DEFAULT '',      -- id поста в канале, если пришёл оттуда
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS variants (
    id         INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    size       TEXT NOT NULL,                  -- 'ONE' для вещей без размера
    quantity   INTEGER NOT NULL DEFAULT 0,
    reserved   INTEGER NOT NULL DEFAULT 0,
    UNIQUE (product_id, size)
);

CREATE TABLE IF NOT EXISTS photos (
    id         INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    file_name  TEXT NOT NULL,                  -- имя файла в data/photos
    position   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS orders (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL,
    username      TEXT NOT NULL DEFAULT '',
    customer_name TEXT NOT NULL DEFAULT '',
    phone         TEXT NOT NULL DEFAULT '',
    delivery      TEXT NOT NULL DEFAULT '',
    address       TEXT NOT NULL DEFAULT '',
    comment       TEXT NOT NULL DEFAULT '',
    total         INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'new', -- new | confirmed | rejected
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS order_items (
    id         INTEGER PRIMARY KEY,
    order_id   INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id INTEGER REFERENCES products(id),
    variant_id INTEGER REFERENCES variants(id),
    -- название и цена копируются в заказ: через год товар переименуют
    -- или переоценят, а заявка должна остаться такой, какой её сделали.
    title      TEXT NOT NULL,
    size       TEXT NOT NULL DEFAULT '',
    price      INTEGER NOT NULL,
    quantity   INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_products_category ON products(category_id);
CREATE INDEX IF NOT EXISTS idx_variants_product ON variants(product_id);
CREATE INDEX IF NOT EXISTS idx_photos_product ON photos(product_id);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
"""

# Категории по умолчанию — те, что назвал владелец, плюс очевидные соседи.
# Переименовать и дополнить можно из админки, слаг остаётся прежним.
DEFAULT_CATEGORIES = [
    ("jackets", "Куртки", 10),
    ("pants", "Штаны", 20),
    ("hoodies", "Кофты и худи", 30),
    ("shirts", "Футболки и рубашки", 40),
    ("shoes", "Обувь", 50),
    ("accessories", "Аксессуары", 60),
]

ONE_SIZE = "ONE"  # вещь без размерной сетки: шапка, сумка, ремень


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Соединение с базой: словари вместо кортежей, внешние ключи включены."""
    config.DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL: чтение витрины не должно ждать, пока админка допишет товар.
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init() -> None:
    """Создаёт таблицы и категории по умолчанию. Повторный вызов безопасен."""
    config.PHOTOS.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        for slug, name, position in DEFAULT_CATEGORIES:
            conn.execute(
                "INSERT OR IGNORE INTO categories (slug, name, position) VALUES (?, ?, ?)",
                (slug, name, position),
            )


# --- категории ---------------------------------------------------------------


def categories() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, slug, name FROM categories ORDER BY position, name"
        ).fetchall()
    return [dict(row) for row in rows]


def category_by_slug(slug: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM categories WHERE slug = ?", (slug,)).fetchone()
    return dict(row) if row else None


# --- товары ------------------------------------------------------------------


def add_product(
    *,
    name: str,
    price: int,
    category_id: int | None = None,
    brand: str = "",
    description: str = "",
    condition: str = "new",
    color: str = "",
    old_price: int | None = None,
    status: str = "active",
    source: str = "admin",
    source_ref: str = "",
    sizes: dict[str, int] | None = None,
    photos: list[str] | None = None,
) -> int:
    """Заводит товар вместе с размерами и фотографиями, возвращает его id.

    `sizes` — «размер → сколько штук»; пустой словарь означает вещь в одном
    экземпляре без размера, самый частый случай в секонде и на ресейле.
    """
    moment = now()
    with connect() as conn:
        cursor = conn.execute(
            """INSERT INTO products
               (name, brand, category_id, price, old_price, description, condition,
                color, status, source, source_ref, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, brand, category_id, price, old_price, description, condition,
             color, status, source, source_ref, moment, moment),
        )
        product_id = int(cursor.lastrowid)
        for size, quantity in (sizes or {ONE_SIZE: 1}).items():
            conn.execute(
                "INSERT OR REPLACE INTO variants (product_id, size, quantity) VALUES (?, ?, ?)",
                (product_id, size.strip().upper() or ONE_SIZE, max(0, int(quantity))),
            )
        for position, file_name in enumerate(photos or []):
            conn.execute(
                "INSERT INTO photos (product_id, file_name, position) VALUES (?, ?, ?)",
                (product_id, file_name, position),
            )
    return product_id


def update_product(product_id: int, **fields: Any) -> None:
    """Меняет отдельные поля товара. Пустой вызов ничего не делает."""
    allowed = {
        "name", "brand", "category_id", "price", "old_price",
        "description", "condition", "color", "status",
    }
    changes = {key: value for key, value in fields.items() if key in allowed}
    if not changes:
        return
    assignments = ", ".join(f"{key} = ?" for key in changes)
    with connect() as conn:
        conn.execute(
            f"UPDATE products SET {assignments}, updated_at = ? WHERE id = ?",
            (*changes.values(), now(), product_id),
        )


def set_quantity(product_id: int, size: str, quantity: int) -> None:
    """Выставляет остаток по размеру. Товар без доступных размеров прячется сам."""
    size = size.strip().upper() or ONE_SIZE
    with connect() as conn:
        conn.execute(
            """INSERT INTO variants (product_id, size, quantity) VALUES (?, ?, ?)
               ON CONFLICT (product_id, size) DO UPDATE SET quantity = excluded.quantity""",
            (product_id, size, max(0, quantity)),
        )
        conn.execute("UPDATE products SET updated_at = ? WHERE id = ?", (now(), product_id))


def add_photo(product_id: int, file_name: str) -> None:
    with connect() as conn:
        position = conn.execute(
            "SELECT COALESCE(MAX(position) + 1, 0) FROM photos WHERE product_id = ?",
            (product_id,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO photos (product_id, file_name, position) VALUES (?, ?, ?)",
            (product_id, file_name, position),
        )


def delete_product(product_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM products WHERE id = ?", (product_id,))


def _collect(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """Дополняет строки товаров размерами и фотографиями — двумя запросами на всё."""
    products = [dict(row) for row in rows]
    if not products:
        return []
    ids = [str(product["id"]) for product in products]
    placeholders = ",".join("?" * len(ids))

    sizes: dict[int, list[dict[str, Any]]] = {}
    for row in conn.execute(
        f"""SELECT product_id, id, size, quantity, reserved
            FROM variants WHERE product_id IN ({placeholders}) ORDER BY id""",
        ids,
    ):
        available = max(0, row["quantity"] - row["reserved"])
        sizes.setdefault(row["product_id"], []).append(
            {"variant_id": row["id"], "size": row["size"], "available": available}
        )

    photos: dict[int, list[str]] = {}
    for row in conn.execute(
        f"""SELECT product_id, file_name FROM photos
            WHERE product_id IN ({placeholders}) ORDER BY position, id""",
        ids,
    ):
        photos.setdefault(row["product_id"], []).append(row["file_name"])

    for product in products:
        product["sizes"] = sizes.get(product["id"], [])
        product["photos"] = photos.get(product["id"], [])
        product["available"] = sum(size["available"] for size in product["sizes"])
    return products


def catalog() -> dict[str, Any]:
    """Всё, что нужно витрине, одним запросом: товары, категории, бренды, размеры.

    Каталог маленький (сотни позиций), поэтому он отдаётся целиком, а фильтры
    работают на стороне мини-приложения: так переключение категории мгновенно,
    без похода в сеть на каждое нажатие.
    """
    with connect() as conn:
        rows = conn.execute(
            """SELECT p.*, c.slug AS category_slug, c.name AS category_name
               FROM products p LEFT JOIN categories c ON c.id = p.category_id
               WHERE p.status = 'active'
               ORDER BY p.created_at DESC"""
        ).fetchall()
        products = _collect(conn, rows)
        category_rows = conn.execute(
            "SELECT slug, name FROM categories ORDER BY position, name"
        ).fetchall()

    # В витрину не попадает то, что уже разобрали: «актуальное наличие» —
    # это то, что можно купить прямо сейчас, а не то, что когда-то было.
    products = [product for product in products if product["available"] > 0]

    used_slugs = {product["category_slug"] for product in products}
    return {
        "categories": [
            dict(row) | {"count": sum(1 for p in products if p["category_slug"] == row["slug"])}
            for row in category_rows
            if row["slug"] in used_slugs
        ],
        "brands": sorted({p["brand"] for p in products if p["brand"]}, key=str.lower),
        "sizes": sorted(
            {s["size"] for p in products for s in p["sizes"] if s["available"] > 0 and s["size"] != ONE_SIZE}
        ),
        "products": [
            {
                "id": p["id"],
                "name": p["name"],
                "brand": p["brand"],
                "category": p["category_slug"],
                "category_name": p["category_name"],
                "price": p["price"],
                "old_price": p["old_price"],
                "description": p["description"],
                "condition": p["condition"],
                "color": p["color"],
                "photos": p["photos"],
                "sizes": [s for s in p["sizes"] if s["available"] > 0],
            }
            for p in products
        ],
    }


def products_for_admin(limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    """Список для админки — вместе со скрытым и распроданным."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT p.*, c.name AS category_name
               FROM products p LEFT JOIN categories c ON c.id = p.category_id
               ORDER BY p.updated_at DESC LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()
        return _collect(conn, rows)


def get_product(product_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            """SELECT p.*, c.name AS category_name, c.slug AS category_slug
               FROM products p LEFT JOIN categories c ON c.id = p.category_id
               WHERE p.id = ?""",
            (product_id,),
        ).fetchone()
        if not row:
            return None
        return _collect(conn, [row])[0]


# --- заказы ------------------------------------------------------------------


class OutOfStock(Exception):
    """Пока покупатель заполнял форму, вещь разобрали."""


def create_order(
    *,
    user_id: int,
    username: str,
    customer_name: str,
    phone: str,
    delivery: str,
    address: str,
    comment: str,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Создаёт заявку и резервирует товар одной транзакцией.

    Резерв проверяется здесь, а не в вебе: между «витрина показала размер»
    и «покупатель нажал оформить» проходят минуты, и за это время вещь могли
    забрать. Если не хватило хотя бы одной позиции — заявка не создаётся вовсе,
    покупатель видит, чего именно не стало.
    """
    if not items:
        raise ValueError("пустой заказ")

    moment = now()
    with connect() as conn:
        prepared: list[dict[str, Any]] = []
        for item in items:
            variant_id = int(item["variant_id"])
            quantity = max(1, int(item.get("quantity", 1)))
            row = conn.execute(
                """SELECT v.id, v.size, v.quantity, v.reserved, p.id AS product_id,
                          p.name, p.brand, p.price, p.status
                   FROM variants v JOIN products p ON p.id = v.product_id
                   WHERE v.id = ?""",
                (variant_id,),
            ).fetchone()
            if row is None or row["status"] != "active":
                raise OutOfStock("товар больше не продаётся")
            if row["quantity"] - row["reserved"] < quantity:
                title = f"{row['brand']} {row['name']}".strip()
                raise OutOfStock(f"«{title}» размера {row['size']} уже нет")
            prepared.append(
                {
                    "variant_id": variant_id,
                    "product_id": row["product_id"],
                    "title": f"{row['brand']} {row['name']}".strip(),
                    "size": row["size"],
                    "price": row["price"],
                    "quantity": quantity,
                }
            )

        total = sum(item["price"] * item["quantity"] for item in prepared)
        cursor = conn.execute(
            """INSERT INTO orders
               (user_id, username, customer_name, phone, delivery, address, comment,
                total, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)""",
            (user_id, username, customer_name, phone, delivery, address, comment,
             total, moment, moment),
        )
        order_id = int(cursor.lastrowid)
        for item in prepared:
            conn.execute(
                """INSERT INTO order_items
                   (order_id, product_id, variant_id, title, size, price, quantity)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (order_id, item["product_id"], item["variant_id"], item["title"],
                 item["size"], item["price"], item["quantity"]),
            )
            conn.execute(
                "UPDATE variants SET reserved = reserved + ? WHERE id = ?",
                (item["quantity"], item["variant_id"]),
            )

    return {"id": order_id, "total": total, "items": prepared, "created_at": moment}


def get_order(order_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            return None
        order = dict(row)
        order["items"] = [
            dict(item)
            for item in conn.execute(
                "SELECT * FROM order_items WHERE order_id = ? ORDER BY id", (order_id,)
            )
        ]
    return order


def set_order_status(order_id: int, status: str) -> dict[str, Any] | None:
    """Подтверждение списывает остаток, отказ снимает резерв.

    Повторное нажатие кнопки ничего не ломает: статус меняется только из `new`,
    а Telegram присылает нажатие дважды чаще, чем кажется.
    """
    if status not in {"confirmed", "rejected"}:
        raise ValueError(f"неизвестный статус заявки: {status}")

    with connect() as conn:
        row = conn.execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()
        if row is None or row["status"] != "new":
            return None

        items = conn.execute(
            "SELECT variant_id, quantity FROM order_items WHERE order_id = ?", (order_id,)
        ).fetchall()
        for item in items:
            if item["variant_id"] is None:
                continue
            if status == "confirmed":
                conn.execute(
                    """UPDATE variants
                       SET reserved = MAX(0, reserved - ?), quantity = MAX(0, quantity - ?)
                       WHERE id = ?""",
                    (item["quantity"], item["quantity"], item["variant_id"]),
                )
            else:
                conn.execute(
                    "UPDATE variants SET reserved = MAX(0, reserved - ?) WHERE id = ?",
                    (item["quantity"], item["variant_id"]),
                )
        conn.execute(
            "UPDATE orders SET status = ?, updated_at = ? WHERE id = ?",
            (status, now(), order_id),
        )

        # Товар, у которого не осталось ни одного размера, уходит из витрины сам:
        # владельцу не нужно помнить, что вещь была последней.
        conn.execute(
            """UPDATE products SET status = 'sold', updated_at = ?
               WHERE status = 'active' AND id IN (
                   SELECT p.id FROM products p
                   JOIN order_items oi ON oi.product_id = p.id
                   WHERE oi.order_id = ?
                   GROUP BY p.id
                   HAVING (SELECT COALESCE(SUM(quantity), 0) FROM variants WHERE product_id = p.id) = 0
               )""",
            (now(), order_id),
        )
    return get_order(order_id)


def orders(status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    query = "SELECT * FROM orders"
    params: list[Any] = []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with connect() as conn:
        return [dict(row) for row in conn.execute(query, params)]


# --- CLI ---------------------------------------------------------------------

DEMO = [
    ("jackets", "Stone Island", "Куртка Garment Dyed Crinkle Reps", 42000, 48000, "used",
     "Оливковая, нашивка на месте, подкладка целая. Сезон 2019.", {"M": 1, "L": 1}),
    ("jackets", "C.P. Company", "Анорак Goggle Chrome-R", 56000, None, "new",
     "Линзы без царапин, полный комплект бирок.", {"L": 1}),
    ("jackets", "Ma.Strum", "Куртка Overshirt", 21000, None, "used",
     "Тёмно-синяя, лёгкая, на весну.", {"M": 1}),
    ("pants", "Stone Island", "Карго Ghost Piece", 24000, None, "new",
     "Чёрные, без принтов, ткань плотная.", {"32": 1, "34": 1}),
    ("pants", "Carhartt WIP", "Double Knee", 9000, 11000, "new",
     "Классические, цвет hamilton brown.", {"32": 2, "34": 1}),
    ("hoodies", "C.P. Company", "Худи Diagonal Fleece", 27000, None, "used",
     "Серое, без катышков, капюшон с линзой.", {"M": 1, "L": 1}),
    ("shirts", "Yohji Yamamoto", "Рубашка из архива", 34000, None, "used",
     "Чёрный хлопок, асимметричный крой.", {"M": 1}),
    ("shoes", "Salomon", "XT-6 Gore-Tex", 19000, None, "new",
     "Полный комплект, коробка есть.", {"42": 1, "43": 1}),
    ("accessories", "Stone Island", "Шапка с нашивкой", 7000, None, "new",
     "Шерсть, тёмно-серая.", {}),
    ("accessories", "Porter Yoshida", "Поясная сумка Tanker", 16000, None, "used",
     "Классический чёрный, все молнии ходят.", {}),
]


def seed_demo() -> int:
    """Наливает десяток товаров, чтобы посмотреть витрину до реального каталога."""
    slugs = {row["slug"]: row["id"] for row in categories()}
    added = 0
    for slug, brand, name, price, old_price, condition, description, sizes in DEMO:
        add_product(
            name=name,
            brand=brand,
            category_id=slugs.get(slug),
            price=price,
            old_price=old_price,
            condition=condition,
            description=description,
            sizes=sizes or {ONE_SIZE: 1},
            source="demo",
        )
        added += 1
    return added


def _report() -> None:
    with connect() as conn:
        counts = {
            "товаров в витрине": conn.execute(
                "SELECT COUNT(*) FROM products WHERE status = 'active'"
            ).fetchone()[0],
            "скрыто и продано": conn.execute(
                "SELECT COUNT(*) FROM products WHERE status != 'active'"
            ).fetchone()[0],
            "штук на остатке": conn.execute(
                "SELECT COALESCE(SUM(quantity - reserved), 0) FROM variants"
            ).fetchone()[0],
            "в резерве": conn.execute(
                "SELECT COALESCE(SUM(reserved), 0) FROM variants"
            ).fetchone()[0],
            "новых заявок": conn.execute(
                "SELECT COUNT(*) FROM orders WHERE status = 'new'"
            ).fetchone()[0],
            "подтверждённых заявок": conn.execute(
                "SELECT COUNT(*) FROM orders WHERE status = 'confirmed'"
            ).fetchone()[0],
        }
    print(f"база: {config.DB_FILE}")
    for label, value in counts.items():
        print(f"  {label}: {value}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="База магазина")
    parser.add_argument("--init", action="store_true", help="создать таблицы и категории")
    parser.add_argument("--demo", action="store_true", help="налить демо-товары")
    parser.add_argument("--check", action="store_true", help="показать, что лежит в базе")
    args = parser.parse_args(argv)

    if not (args.init or args.demo or args.check):
        parser.print_help()
        return 0

    if args.init or args.demo:
        init()
        print(f"база готова: {config.DB_FILE}")
    if args.demo:
        print(f"добавлено демо-товаров: {seed_demo()}")
    if args.check:
        if not config.DB_FILE.exists():
            print("базы ещё нет — запусти python -m src.db --init")
            return 1
        _report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
