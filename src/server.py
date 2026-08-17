"""HTTP-часть магазина: витрина, каталог и приём заказов.

Живёт в том же процессе, что и бот (см. `src/app.py`): для магазина на сотню
заказов в месяц разносить их по разным сервисам — лишние движущиеся части,
которые придётся поднимать и чинить по отдельности.

Главное здесь — **проверка подписи** `initData`. Мини-приложение открыто по
обычному адресу, и без проверки кто угодно мог бы прислать заявку от чужого
имени или от имени владельца. Telegram подписывает данные о пользователе
HMAC-SHA256 на ключе, выведенном из токена бота; сервер эту подпись
пересчитывает и сверяет. Без валидной подписи заказ не принимается.

Ручки:

    GET  /app/            витрина мини-приложения
    GET  /photos/<файл>   фотографии товаров
    GET  /api/catalog     каталог, категории, бренды, размеры
    POST /api/order       заявка: проверка подписи, резерв, письмо владельцу
    GET  /healthz         жив ли процесс (для сторожа на сервере)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
from typing import Any, Awaitable, Callable

from aiohttp import web

from . import config, db

# Подпись Telegram считается протухшей через сутки: столько мини-приложение
# может провисеть открытым в фоне, дольше — уже подозрительно.
INIT_DATA_TTL = 24 * 60 * 60

# Простой заслон от лавины заявок с одного аккаунта: пять штук за десять минут.
# Больше живой человек не оформит, а бот — запросто.
ORDER_LIMIT = 5
ORDER_WINDOW = 10 * 60
# Словарь попыток живёт в памяти процесса. Чтобы он не рос до перезапуска,
# при переполнении из него выметаются те, чьё окно давно закрылось.
ORDER_MEMORY = 1000

_recent_orders: dict[int, list[float]] = {}

# Типизированные ключи приложения: aiohttp иначе ругается на строковые,
# а в новых версиях обещает их запретить.
SETTINGS = web.AppKey("settings", config.Settings)
NOTIFY_ORDER = web.AppKey("notify_order", object)
ALLOW_UNSIGNED = web.AppKey("allow_unsigned", bool)


def verify_init_data(init_data: str, bot_token: str) -> dict[str, Any] | None:
    """Проверяет подпись мини-приложения и возвращает данные пользователя.

    Возвращает None на любой неудаче — вызывающему коду не нужно знать,
    подпись битая, устарела или её вовсе нет.
    """
    if not init_data or not bot_token:
        return None
    try:
        # keep_blank_values: в подпись входят все поля, что пришли, включая пустые.
        # Выбросив пустое поле, мы посчитали бы другую строку и отвергли живого
        # покупателя как самозванца.
        pairs = dict(
            urllib.parse.parse_qsl(init_data, strict_parsing=True, keep_blank_values=True)
        )
    except ValueError:
        return None

    received_hash = pairs.pop("hash", "")
    if not received_hash:
        return None

    check_string = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        return None

    auth_date = pairs.get("auth_date", "0")
    if not auth_date.isdigit() or time.time() - int(auth_date) > INIT_DATA_TTL:
        return None

    try:
        user = json.loads(pairs.get("user", "{}"))
    except json.JSONDecodeError:
        return None
    # Подпись без пользователя бывает: витрину открыли не из лички, а из канала.
    # Такую заявку принимать нельзя — бот не сможет ответить покупателю.
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        return None
    return user


def _user_from_request(request: web.Request) -> dict[str, Any] | None:
    settings = request.app[SETTINGS]
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    user = verify_init_data(init_data, settings.bot_token)
    if user is None and request.app[ALLOW_UNSIGNED]:
        # Режим отладки в обычном браузере: подписи нет, потому что нет Telegram.
        # Включается только переменной DEV_ALLOW_UNSIGNED=1 — на сервере не ставить.
        return {"id": 0, "first_name": "Отладка", "username": "debug"}
    return user


def _rate_limited(user_id: int) -> bool:
    now = time.time()
    if len(_recent_orders) > ORDER_MEMORY:
        for other, moments in list(_recent_orders.items()):
            if all(now - moment >= ORDER_WINDOW for moment in moments):
                del _recent_orders[other]
    attempts = [moment for moment in _recent_orders.get(user_id, []) if now - moment < ORDER_WINDOW]
    _recent_orders[user_id] = attempts
    if len(attempts) >= ORDER_LIMIT:
        return True
    attempts.append(now)
    return False


async def handle_catalog(request: web.Request) -> web.Response:
    """Каталог целиком: витрина фильтрует его на устройстве.

    Подпись здесь не требуется — это те же вещи, что магазин показывает
    в открытом канале. Требовать её значило бы сломать отладку в браузере
    ради данных, которые и так публичны.
    """
    settings = request.app[SETTINGS]
    return web.json_response(
        {
            "shop_name": settings.shop_name,
            "currency": settings.currency,
            "delivery_options": list(settings.delivery_options),
            "catalog": db.catalog(),
        },
        dumps=lambda payload: json.dumps(payload, ensure_ascii=False),
    )


async def handle_order(request: web.Request) -> web.Response:
    """Принимает заявку: проверяет подпись, резервирует товар, зовёт владельца."""
    user = _user_from_request(request)
    if user is None:
        return web.json_response({"error": "Открой магазин через бота"}, status=401)

    try:
        payload = await request.json()
    except ValueError:  # битый JSON или байты не в UTF-8
        return web.json_response({"error": "Битый запрос"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"error": "Битый запрос"}, status=400)

    items = payload.get("items") or []
    if not isinstance(items, list) or not items:
        return web.json_response({"error": "Корзина пуста"}, status=400)
    if len(items) > 50:
        return web.json_response({"error": "Слишком много позиций в заказе"}, status=400)

    name = str(payload.get("customer_name", "")).strip()[:80]
    phone = str(payload.get("phone", "")).strip()[:30]
    if len(name) < 2:
        return web.json_response({"error": "Как к тебе обращаться?"}, status=400)
    if sum(character.isdigit() for character in phone) < 10:
        return web.json_response({"error": "Телефон не похож на настоящий"}, status=400)

    user_id = int(user.get("id", 0))
    if _rate_limited(user_id):
        return web.json_response({"error": "Слишком много заявок подряд. Напиши боту"}, status=429)

    try:
        wanted = [
            {"variant_id": int(item["variant_id"]), "quantity": int(item.get("quantity", 1))}
            for item in items
            if isinstance(item, dict) and str(item.get("variant_id", "")).lstrip("-").isdigit()
        ]
        # Тихо выкинуть непонятную позицию нельзя: покупатель отправил три вещи,
        # владелец увидел бы две и отдал не то, о чём договаривались.
        if len(wanted) != len(items):
            raise ValueError("часть позиций не разобрал")

        order = db.create_order(
            user_id=user_id,
            username=str(user.get("username", "")),
            customer_name=name,
            phone=phone,
            delivery=str(payload.get("delivery", "")).strip()[:60],
            address=str(payload.get("address", "")).strip()[:200],
            comment=str(payload.get("comment", "")).strip()[:300],
            items=wanted,
        )
    except db.OutOfStock as error:
        return web.json_response({"error": str(error)}, status=409)
    except (KeyError, ValueError, TypeError):
        return web.json_response({"error": "Не разобрал заказ"}, status=400)

    notify: Callable[[int], Awaitable[None]] = request.app[NOTIFY_ORDER]
    try:
        await notify(order["id"])
    except Exception as error:  # noqa: BLE001 — заявка уже в базе, её нельзя терять
        # Владелец не получил письмо, но заказ сохранён: увидит его в /orders.
        request.app.logger.exception("не удалось отправить заявку владельцу: %s", error)

    return web.json_response({"ok": True, "order_id": order["id"], "total": order["total"]})


async def handle_index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(
        config.WEBAPP / "index.html",
        headers={"Cache-Control": "no-cache"},
    )


async def handle_root(request: web.Request) -> web.Response:
    """Корень ведёт в витрину: по адресу без /app/ приходят из закладок и ссылок."""
    raise web.HTTPFound("/app/")


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "products": len(db.catalog()["products"])})


@web.middleware
async def no_cache_for_webapp(request: web.Request, handler: Callable) -> web.StreamResponse:
    """Витрина не кэшируется, фотографии кэшируются надолго.

    Иначе после правки `app.js` владелец видит старую витрину и решает, что
    ничего не изменилось, — а фото по имени файла не меняются никогда.
    """
    response = await handler(request)
    if request.path.startswith("/app"):
        response.headers.setdefault("Cache-Control", "no-cache")
    elif request.path.startswith("/photos"):
        response.headers.setdefault("Cache-Control", "public, max-age=604800")
    return response


def create_app(
    settings: config.Settings,
    notify_order: Callable[[int], Awaitable[None]],
    allow_unsigned: bool = False,
) -> web.Application:
    app = web.Application(middlewares=[no_cache_for_webapp])
    app[SETTINGS] = settings
    app[NOTIFY_ORDER] = notify_order
    app[ALLOW_UNSIGNED] = allow_unsigned

    config.PHOTOS.mkdir(parents=True, exist_ok=True)

    app.router.add_get("/", handle_root)
    app.router.add_get("/app/", handle_index)
    app.router.add_get("/healthz", handle_health)
    app.router.add_get("/api/catalog", handle_catalog)
    app.router.add_post("/api/order", handle_order)
    app.router.add_static("/app/", config.WEBAPP)
    app.router.add_static("/photos/", config.PHOTOS)
    return app
