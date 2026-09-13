"""Тесты на то, что стоит денег и репутации: остатки, подпись, разбор постов.

Витрина проверяется глазами, а вот это глазами не проверишь: двойная продажа
последней вещи, подделанная подпись, цена, угаданная из поста неправильно,
резерв, который не вернулся после отказа, и заявка, которую Telegram отказался
доставить владельцу из-за угловой скобки в имени покупателя.

Веб-часть проверяется целиком, живым сервером: это единственная дверь,
в которую стучится кто угодно из интернета.

    python -m unittest discover tests
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import io
import json
import tempfile
import time
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from src import admin, bot, config, db, importer, music, scrape, server

TOKEN = "123456:TESTTOKEN"


def signed_init_data(user_id: int = 42, age_seconds: int = 0, with_user: bool = True) -> str:
    """Собирает initData так же, как это делает Telegram.

    `with_user=False` — витрину открыли не из лички, а из канала: подпись
    настоящая, а пользователя в данных нет.
    """
    pairs = {
        "auth_date": str(int(time.time()) - age_seconds),
        "query_id": "AAH",
    }
    if with_user:
        pairs["user"] = json.dumps({"id": user_id, "first_name": "Тест"}, ensure_ascii=False)
    check = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(pairs)


def настройки() -> config.Settings:
    """Настройки магазина для тестов: токен тот же, которым подписан initData."""
    return config.Settings(
        bot_token=TOKEN,
        admin_ids=(1,),
        webapp_url="https://example.org/app/",
        shop_name="Mco shop",
        port=8080,
        delivery_options=("Самовывоз", "СДЭК"),
        currency="₽",
    )


class БазаНаВремя(unittest.TestCase):
    """Каждый тест работает со своей базой, папкой фото и музыкой во временной папке."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self._paths = (config.DATA, config.PHOTOS, config.MUSIC, config.DB_FILE)
        config.DATA = Path(self._temp.name)
        config.PHOTOS = config.DATA / "photos"
        config.MUSIC = config.DATA / "music"
        config.MUSIC.mkdir()
        config.DB_FILE = config.DATA / "shop.db"
        db.init()

    def tearDown(self) -> None:
        config.DATA, config.PHOTOS, config.MUSIC, config.DB_FILE = self._paths
        self._temp.cleanup()


class ТестыОстатков(БазаНаВремя):
    def _товар(self, sizes: dict[str, int] | None = None) -> dict:
        product_id = db.add_product(name="Куртка", brand="Stone Island", price=42000,
                                    sizes=sizes or {"M": 1})
        return db.get_product(product_id)

    def заказать(self, variant_id: int, quantity: int = 1) -> dict:
        return db.create_order(
            user_id=1, username="tester", customer_name="Тест", phone="+79001234567",
            delivery="Самовывоз", address="", comment="",
            items=[{"variant_id": variant_id, "quantity": quantity}],
        )

    def test_последнюю_вещь_нельзя_купить_дважды(self) -> None:
        variant_id = self._товар()["sizes"][0]["variant_id"]
        self.заказать(variant_id)
        with self.assertRaises(db.OutOfStock):
            self.заказать(variant_id)

    def test_отказ_возвращает_вещь_в_витрину(self) -> None:
        variant_id = self._товар()["sizes"][0]["variant_id"]
        order = self.заказать(variant_id)
        db.set_order_status(order["id"], "rejected")
        self.assertEqual(len(db.catalog()["products"]), 1)
        # После отказа ту же вещь можно заказать снова — резерв снят.
        self.заказать(variant_id)

    def test_подтверждение_списывает_и_прячет_проданное(self) -> None:
        product = self._товар()
        order = self.заказать(product["sizes"][0]["variant_id"])
        db.set_order_status(order["id"], "confirmed")
        self.assertEqual(db.get_product(product["id"])["status"], "sold")
        self.assertEqual(db.catalog()["products"], [])

    def test_остаётся_в_витрине_пока_есть_другой_размер(self) -> None:
        product = self._товар({"M": 1, "L": 1})
        order = self.заказать(product["sizes"][0]["variant_id"])
        db.set_order_status(order["id"], "confirmed")
        self.assertEqual(db.get_product(product["id"])["status"], "active")
        self.assertEqual([s["size"] for s in db.catalog()["products"][0]["sizes"]], ["L"])

    def test_повторное_нажатие_кнопки_ничего_не_меняет(self) -> None:
        product = self._товар({"M": 2})
        order = self.заказать(product["sizes"][0]["variant_id"])
        db.set_order_status(order["id"], "confirmed")
        self.assertIsNone(db.set_order_status(order["id"], "rejected"))
        self.assertEqual(db.get_product(product["id"])["sizes"][0]["available"], 1)

    def test_витрина_не_показывает_зарезервированное(self) -> None:
        product = self._товар({"M": 1})
        self.заказать(product["sizes"][0]["variant_id"])
        self.assertEqual(db.catalog()["products"], [])

    def test_одна_вещь_дважды_в_одной_заявке_не_проходит(self) -> None:
        # Витрина складывает одинаковые позиции сама, но заявка приходит по HTTP:
        # две строки по штуке на последнюю куртку — это попытка купить её дважды.
        variant_id = self._товар({"M": 1})["sizes"][0]["variant_id"]
        with self.assertRaises(db.OutOfStock):
            db.create_order(
                user_id=1, username="", customer_name="Тест", phone="+79001234567",
                delivery="", address="", comment="",
                items=[{"variant_id": variant_id, "quantity": 1},
                       {"variant_id": variant_id, "quantity": 1}],
            )
        self.assertEqual(db.orders(), [])

    def test_одинаковые_позиции_складываются_в_одну(self) -> None:
        product = self._товар({"M": 2})
        variant_id = product["sizes"][0]["variant_id"]
        order = db.create_order(
            user_id=1, username="", customer_name="Тест", phone="+79001234567",
            delivery="", address="", comment="",
            items=[{"variant_id": variant_id, "quantity": 1},
                   {"variant_id": variant_id, "quantity": 1}],
        )
        self.assertEqual(len(order["items"]), 1)
        self.assertEqual(order["items"][0]["quantity"], 2)
        self.assertEqual(order["total"], 42000 * 2)
        self.assertEqual(db.get_product(product["id"])["sizes"][0]["available"], 0)

    def test_удаление_заказанного_товара_не_ломает_заявку(self) -> None:
        # Владелец удаляет вещь, на которую есть заявка: заявка должна остаться
        # читаемой, а кнопка «Удалить» — работать, а не падать на внешнем ключе.
        product = self._товар()
        order = self.заказать(product["sizes"][0]["variant_id"])
        db.delete_product(product["id"])
        saved = db.get_order(order["id"])
        self.assertEqual(saved["items"][0]["title"], "Stone Island Куртка")
        self.assertIsNone(saved["items"][0]["product_id"])
        # Заявку по удалённому товару всё ещё можно закрыть — списывать нечего.
        self.assertEqual(db.set_order_status(order["id"], "confirmed")["status"], "confirmed")

    def test_скрытый_товар_заказать_нельзя(self) -> None:
        product = self._товар()
        db.update_product(product["id"], status="hidden")
        with self.assertRaises(db.OutOfStock):
            self.заказать(product["sizes"][0]["variant_id"])

    def test_несуществующий_вариант_не_создаёт_заявку(self) -> None:
        with self.assertRaises(db.OutOfStock):
            self.заказать(99999)
        self.assertEqual(db.orders(), [])

    def test_пустая_заявка_не_создаётся(self) -> None:
        with self.assertRaises(ValueError):
            db.create_order(
                user_id=1, username="", customer_name="Тест", phone="+79001234567",
                delivery="", address="", comment="", items=[],
            )

    def test_забытая_заявка_возвращает_вещь_в_витрину(self) -> None:
        # Владелец не ответил трое суток: вещь не должна пропасть из продажи.
        product = self._товар()
        order = self.заказать(product["sizes"][0]["variant_id"])
        self.assertEqual(db.catalog()["products"], [])
        self._состарить(order["id"], hours=db.STALE_HOURS + 1)

        self.assertEqual(len(db.catalog()["products"]), 1)
        self.assertEqual(db.get_order(order["id"])["status"], "expired")
        # Протухшую заявку кнопками уже не закрыть: вещь снова продаётся.
        self.assertIsNone(db.set_order_status(order["id"], "confirmed"))

    def test_свежая_заявка_держит_резерв(self) -> None:
        product = self._товар()
        order = self.заказать(product["sizes"][0]["variant_id"])
        self._состарить(order["id"], hours=db.STALE_HOURS - 1)
        self.assertEqual(db.catalog()["products"], [])
        self.assertEqual(db.get_order(order["id"])["status"], "new")

    def _состарить(self, order_id: int, hours: int) -> None:
        """Двигает дату заявки в прошлое — иначе протухания пришлось бы ждать сутками."""
        moment = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
        with db.connect() as conn:
            conn.execute("UPDATE orders SET created_at = ? WHERE id = ?", (moment, order_id))

    def test_остаток_из_админки_обнуляет_исчезнувший_размер(self) -> None:
        product = self._товар({"M": 1, "L": 1})
        db.set_quantity(product["id"], "M", 0)
        sizes = {s["size"]: s["available"] for s in db.get_product(product["id"])["sizes"]}
        self.assertEqual(sizes, {"M": 0, "L": 1})
        self.assertEqual([s["size"] for s in db.catalog()["products"][0]["sizes"]], ["L"])


class ТестыПодписи(unittest.TestCase):
    def test_валидная_подпись_даёт_пользователя(self) -> None:
        user = server.verify_init_data(signed_init_data(), TOKEN)
        self.assertEqual(user["id"], 42)

    def test_чужой_токен_не_проходит(self) -> None:
        self.assertIsNone(server.verify_init_data(signed_init_data(), "999:OTHER"))

    def test_подделанные_данные_не_проходят(self) -> None:
        # Подменяем пользователя, оставляя подпись от прежних данных:
        # ровно так выглядела бы попытка оформить заказ от чужого имени.
        pairs = dict(urllib.parse.parse_qsl(signed_init_data(user_id=42)))
        pairs["user"] = json.dumps({"id": 43, "first_name": "Чужой"}, ensure_ascii=False)
        self.assertIsNone(server.verify_init_data(urllib.parse.urlencode(pairs), TOKEN))

    def test_старая_подпись_не_проходит(self) -> None:
        self.assertIsNone(server.verify_init_data(signed_init_data(age_seconds=90000), TOKEN))

    def test_пустая_строка_не_проходит(self) -> None:
        self.assertIsNone(server.verify_init_data("", TOKEN))

    def test_мусор_вместо_данных_не_проходит(self) -> None:
        self.assertIsNone(server.verify_init_data("вообще не query-строка", TOKEN))

    def test_подпись_без_пользователя_не_проходит(self) -> None:
        # Подпись настоящая, но заявку не к кому привязать: бот не сможет ответить.
        self.assertIsNone(server.verify_init_data(signed_init_data(with_user=False), TOKEN))

    def test_пустое_поле_не_ломает_проверку(self) -> None:
        # В подпись входят все поля, что пришли, включая пустые: выброшенное
        # пустое поле дало бы другую строку и отказ живому покупателю.
        pairs = {
            "auth_date": str(int(time.time())),
            "start_param": "",
            "user": json.dumps({"id": 42, "first_name": "Тест"}, ensure_ascii=False),
        }
        check = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
        secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        pairs["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        user = server.verify_init_data(urllib.parse.urlencode(pairs), TOKEN)
        self.assertEqual(user["id"], 42)


    def test_строка_без_пар_ключ_значение_не_проходит(self) -> None:
        self.assertIsNone(server.verify_init_data("мусор", TOKEN))

    def test_данные_без_подписи_вообще_не_проходят(self) -> None:
        данные = signed_init_data()
        без_подписи = "&".join(
            пара for пара in данные.split("&") if not пара.startswith("hash=")
        )
        self.assertIsNone(server.verify_init_data(без_подписи, TOKEN))

    def test_пользователь_не_json_не_проходит(self) -> None:
        pairs = {"auth_date": str(int(time.time())), "user": "{это не json"}
        check = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
        secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        pairs["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        self.assertIsNone(server.verify_init_data(urllib.parse.urlencode(pairs), TOKEN))


class ТестыРазбораПостов(unittest.TestCase):
    def test_цена_в_разных_написаниях(self) -> None:
        self.assertEqual(importer.find_price("Цена 42 000 ₽"), 42000)
        self.assertEqual(importer.find_price("9000 руб"), 9000)
        self.assertEqual(importer.find_price("отдам за 12к"), 12000)
        self.assertIsNone(importer.find_price("Коллекция 2019 года"))

    def test_бренд_длиннее_важнее(self) -> None:
        self.assertEqual(importer.find_brand("Carhartt WIP Double Knee"), "Carhartt WIP")
        self.assertEqual(importer.find_brand("Просто куртка"), "")

    def test_размеры_из_поста(self) -> None:
        self.assertEqual(importer.find_sizes("размер L"), {"L": 1})
        self.assertEqual(importer.find_sizes("размеры 32/34"), {"32": 1, "34": 1})
        self.assertEqual(importer.find_sizes("тёплая куртка"), {})

    def test_размеры_из_админки(self) -> None:
        from src.admin import parse_sizes
        self.assertEqual(parse_sizes("M:2 L"), {"M": 2, "L": 1})
        self.assertEqual(parse_sizes("-"), {db.ONE_SIZE: 1})


class ТестыСбораИзКанала(unittest.TestCase):
    """Формат карточек канала: цена с точкой, повтор размера, ответы-обновления."""

    def товар(self, post_id: int = 71, text: str = "") -> dict:
        return scrape.parse_product({
            "id": post_id,
            "text": text or (
                "Rick Owens DRKSHDW x Converse DBL Drkstar\n"
                "Размеры(EUR): 41, 41, 42\n"
                "Состояние: NEW, полный комплект!\n"
                "Стоимость: 13.990₽\n"
                "Купить: @sfmmfu"
            ),
            "photos": ["https://cdn/1.jpg"],
            "date": "2026-07-10T08:34:56+00:00",
            "reply_to": None,
        })

    def test_цена_с_точкой_как_разделителем_тысяч(self) -> None:
        # «13.990₽» — это почти четырнадцать тысяч, а не девятьсот девяносто.
        self.assertEqual(scrape.parse_money("Стоимость: 13.990₽"), 13990)
        self.assertEqual(scrape.parse_money("12 000 ₽"), 12000)
        self.assertEqual(scrape.parse_money("8.000₽"), 8000)

    def test_повтор_размера_это_количество(self) -> None:
        self.assertEqual(self.товар()["sizes"], {"41": 2, "42": 1})

    def test_бренд_отделяется_от_названия(self) -> None:
        product = self.товар(text="STONE ISLAND TEDDY FLEECE\nРазмер: XL\nСтоимость: 34.990₽")
        self.assertEqual(product["brand"], "Stone Island")
        self.assertEqual(product["name"], "TEDDY FLEECE")

    def test_пост_без_размера_не_товар(self) -> None:
        # «Можем привезти под заказ такие сумки, стоимость 12.000₽» — не наличие.
        self.assertIsNone(self.товар(text="Можем привезти сумки\nСтоимость 12.000₽"))

    def test_продажа_размера_убирает_одну_штуку(self) -> None:
        products = {71: self.товар()}
        scrape.apply_updates(products, [
            {"id": 115, "reply_to": 71, "text": "❗️41 ПРОДАНО❗️", "photos": [], "date": ""},
        ])
        self.assertEqual(products[71]["sizes"], {"41": 1, "42": 1})

    def test_продано_без_размера_закрывает_вещь(self) -> None:
        products = {71: self.товар()}
        scrape.apply_updates(products, [
            {"id": 116, "reply_to": 71, "text": "❗️ПРОДАНО❗️", "photos": [], "date": ""},
        ])
        self.assertTrue(products[71]["sold"])

    def test_новая_цена_переносит_старую_в_зачёркнутую(self) -> None:
        products = {71: self.товар()}
        scrape.apply_updates(products, [
            {"id": 234, "reply_to": 71, "text": "❗️13.990₽❗️\n8.000₽ - новая цена",
             "photos": [], "date": ""},
        ])
        self.assertEqual(products[71]["price"], 8000)
        self.assertEqual(products[71]["old_price"], 13990)

    def test_живой_комментарий_остаётся_человеку(self) -> None:
        products = {71: self.товар()}
        scrape.apply_updates(products, [
            {"id": 233, "reply_to": 71, "text": "Привезли ещё пару размеров, 5 пар в наличии!",
             "photos": [], "date": ""},
        ])
        self.assertEqual(products[71]["sizes"], {"41": 2, "42": 1})
        self.assertTrue(products[71]["notes"])


class ТестыВебАПИ(БазаНаВремя, unittest.IsolatedAsyncioTestCase):
    """Живой сервер, живые запросы: витрина, каталог и приём заявки.

    Проверяется то, что видно снаружи: без подписи заявку не принимают,
    подделанную — тоже, а разобранная не до конца корзина не превращается
    в заявку, где не хватает вещей.
    """

    async def asyncSetUp(self) -> None:
        self.product_id = db.add_product(
            name="Куртка", brand="Stone Island", price=42000, sizes={"M": 1}
        )
        self.variant_id = db.get_product(self.product_id)["sizes"][0]["variant_id"]

        self.notified: list[int] = []
        self.уведомление_падает = False
        server._recent_orders.clear()  # лимит заявок живёт в процессе, а не в базе
        application = server.create_app(настройки(), notify_order=self.заметить)
        self.client = TestClient(TestServer(application))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()

    async def заметить(self, order_id: int) -> None:
        if self.уведомление_падает:
            raise RuntimeError("Telegram недоступен")
        self.notified.append(order_id)

    async def оформить(self, init_data: str | None = None, **payload):
        body = {
            "customer_name": "Тест",
            "phone": "+7 900 123-45-67",
            "delivery": "Самовывоз",
            "address": "",
            "comment": "",
            "items": [{"variant_id": self.variant_id, "quantity": 1}],
        }
        body.update(payload)
        headers = {"X-Telegram-Init-Data": signed_init_data() if init_data is None else init_data}
        return await self.client.post("/api/order", json=body, headers=headers)

    async def test_каталог_открыт_без_подписи(self) -> None:
        response = await self.client.get("/api/catalog")
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertEqual(data["shop_name"], "Mco shop")
        self.assertEqual(len(data["catalog"]["products"]), 1)

    async def test_витрина_отдаётся(self) -> None:
        response = await self.client.get("/app/")
        self.assertEqual(response.status, 200)
        self.assertIn("text/html", response.headers["Content-Type"])

    async def test_витрину_клиент_не_хранит(self) -> None:
        """Обрезанная копия, осевшая в WebKit, не открывалась месяцами.

        Страница и `app.js` попались на этом по очереди, поэтому проверяются
        оба: `no-store` должен стоять на всём /app, а не на одной странице.
        """
        for path in ("/app/", "/app/app.js"):
            with self.subTest(path=path):
                response = await self.client.get(path)
                self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_заявка_с_подписью_принимается(self) -> None:
        response = await self.оформить(comment="позвоните вечером")
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertEqual(data["total"], 42000)

        order = db.get_order(data["order_id"])
        self.assertEqual(order["status"], "new")
        self.assertEqual(order["user_id"], 42)
        self.assertEqual(order["comment"], "позвоните вечером")
        self.assertEqual(self.notified, [data["order_id"]])
        # Вещь ушла в резерв, значит из витрины исчезла.
        self.assertEqual(db.catalog()["products"], [])

    async def test_без_подписи_не_принимает(self) -> None:
        response = await self.оформить(init_data="")
        self.assertEqual(response.status, 401)
        self.assertEqual(db.orders(), [])

    async def test_подделанная_подпись_не_принимает(self) -> None:
        pairs = dict(urllib.parse.parse_qsl(signed_init_data(user_id=42)))
        pairs["user"] = json.dumps({"id": 43, "first_name": "Чужой"}, ensure_ascii=False)
        response = await self.оформить(init_data=urllib.parse.urlencode(pairs))
        self.assertEqual(response.status, 401)
        self.assertEqual(db.orders(), [])

    async def test_пустая_корзина_не_принимается(self) -> None:
        self.assertEqual((await self.оформить(items=[])).status, 400)

    async def test_имя_и_телефон_проверяются(self) -> None:
        self.assertEqual((await self.оформить(customer_name="")).status, 400)
        self.assertEqual((await self.оформить(phone="звоните в канал")).status, 400)
        self.assertEqual(db.orders(), [])

    async def test_битая_позиция_отклоняет_всю_заявку(self) -> None:
        # Молча выкинуть непонятную позицию нельзя: владелец соберёт не тот заказ.
        response = await self.оформить(items=[
            {"variant_id": self.variant_id, "quantity": 1},
            {"variant_id": "какой-то"},
        ])
        self.assertEqual(response.status, 400)
        self.assertEqual(db.orders(), [])

    async def test_слишком_длинная_корзина_не_принимается(self) -> None:
        позиции = [{"variant_id": self.variant_id, "quantity": 1} for _ in range(51)]
        response = await self.оформить(items=позиции)
        self.assertEqual(response.status, 400)
        self.assertIn("Слишком много", (await response.json())["error"])

    async def test_разобранную_вещь_не_продать_второму(self) -> None:
        self.assertEqual((await self.оформить()).status, 200)
        second = await self.оформить()
        self.assertEqual(second.status, 409)
        self.assertIn("уже нет", (await second.json())["error"])

    async def test_дважды_одна_вещь_в_корзине_не_проходит(self) -> None:
        response = await self.оформить(items=[
            {"variant_id": self.variant_id, "quantity": 1},
            {"variant_id": self.variant_id, "quantity": 1},
        ])
        self.assertEqual(response.status, 409)
        self.assertEqual(db.orders(), [])

    async def test_лавина_заявок_упирается_в_лимит(self) -> None:
        for _ in range(server.ORDER_LIMIT):
            await self.оформить()
        self.assertEqual((await self.оформить()).status, 429)

    async def test_упавшее_уведомление_не_теряет_заявку(self) -> None:
        # Telegram может лежать, а заявка уже в базе: покупателю отвечаем «принято»,
        # владелец найдёт её в /orders. Терять оплаченное внимание нельзя.
        self.уведомление_падает = True
        with self.assertLogs("aiohttp.web", level="ERROR"):
            response = await self.оформить()
        self.assertEqual(response.status, 200)
        self.assertEqual(len(db.orders()), 1)

    async def test_не_объект_вместо_заявки_не_роняет_сервер(self) -> None:
        # В /api/order можно прислать что угодно: список, строку, обрезанный JSON.
        for body in (b"[1, 2, 3]", '"привет"'.encode(), "{это не json".encode(), b"\xff\xfe"):
            response = await self.client.post(
                "/api/order", data=body,
                headers={"Content-Type": "application/json",
                         "X-Telegram-Init-Data": signed_init_data()},
            )
            self.assertEqual(response.status, 400, body)
        self.assertEqual(db.orders(), [])

    async def test_корень_ведёт_в_витрину(self) -> None:
        response = await self.client.get("/", allow_redirects=False)
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers["Location"], "/app/")

    async def test_healthz_отвечает(self) -> None:
        data = await (await self.client.get("/healthz")).json()
        self.assertEqual(data, {"ok": True, "products": 1})


class ТестыЗаливкиЧерновика(БазаНаВремя):
    def test_битый_черновик_не_обрывает_импорт(self) -> None:
        # Черновик правят руками: «12 000» вместо 12000 — обычная опечатка,
        # и остальной каталог из-за неё залиться должен.
        drafts = [
            {"post_id": 1, "name": "Куртка", "price": "12 000", "sizes": {"M": 1}},
            {"post_id": 2, "name": "Худи", "price": 9000, "sizes": {"L": 1}},
        ]
        with contextlib.redirect_stdout(io.StringIO()) as вывод:
            added = importer.apply_drafts(drafts)
        self.assertEqual(added, 1)
        self.assertIn("№1 пропущен", вывод.getvalue())
        self.assertEqual([p["name"] for p in db.catalog()["products"]], ["Худи"])


class ТестыСообщений(БазаНаВремя):
    """Тексты уходят с parse_mode=HTML: чужие угловые скобки ломают доставку."""

    def test_имя_покупателя_не_ломает_заявку(self) -> None:
        product_id = db.add_product(name="Куртка", brand="Stone Island", price=42000)
        variant_id = db.get_product(product_id)["sizes"][0]["variant_id"]
        order = db.create_order(
            user_id=1, username="<script>", customer_name="Вася <b>",
            phone="+79001234567", delivery="", address="", comment="а можно <i>дешевле",
            items=[{"variant_id": variant_id, "quantity": 1}],
        )
        text = bot.order_text(db.get_order(order["id"]), настройки())
        self.assertIn("Вася &lt;b&gt;", text)
        self.assertIn("&lt;i&gt;дешевле", text)
        self.assertNotIn("<b>", text.replace("<b>Заявка", ""))
        self.assertNotIn("<script>", text)

    def test_название_товара_из_канала_не_ломает_список(self) -> None:
        product_id = db.add_product(name="Куртка <XL>", brand="Ma.Strum", price=21000)
        line = admin._product_line(db.get_product(product_id), настройки())
        self.assertIn("Куртка &lt;XL&gt;", line)
        self.assertNotIn("<XL>", line)


class ТестыОтладочногоРежима(БазаНаВремя, unittest.IsolatedAsyncioTestCase):
    """DEV_ALLOW_UNSIGNED: витрина открывается в обычном браузере, без Telegram.

    Режим существует ради отладки и он же — дыра, если попадёт на сервер:
    заявку принимают вообще без подписи. Поэтому проверяется явно.
    """

    async def asyncSetUp(self) -> None:
        product_id = db.add_product(name="Куртка", brand="Stone Island", price=42000)
        self.variant_id = db.get_product(product_id)["sizes"][0]["variant_id"]
        server._recent_orders.clear()
        application = server.create_app(
            настройки(), notify_order=self.заметить, allow_unsigned=True
        )
        self.client = TestClient(TestServer(application))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()

    async def заметить(self, order_id: int) -> None:
        self.order_id = order_id

    async def test_заявка_без_подписи_принимается_и_подписывается_отладкой(self) -> None:
        response = await self.client.post("/api/order", json={
            "customer_name": "Отладка",
            "phone": "+7 900 123-45-67",
            "items": [{"variant_id": self.variant_id, "quantity": 1}],
        })
        self.assertEqual(response.status, 200)
        order = db.get_order((await response.json())["order_id"])
        self.assertEqual(order["user_id"], 0)

    async def test_витрина_узнаёт_про_плейлист_из_каталога(self) -> None:
        """Песни и ритм каждой приезжают с каталогом — отдельного запроса нет."""
        каталог = await (await self.client.get("/api/catalog")).json()
        self.assertEqual(каталог["music"], [])  # музыки нет, в витрине тихо

        for имя, bpm in (("track-1.mp3", 73.5), ("track-2.mp3", 174.0)):
            (config.MUSIC / имя).write_bytes(b"\xff\xfb\x90")
            music.add(имя, имя, {"bpm": bpm, "flip_ms": 3265, "offset_ms": 1780})

        каталог = await (await self.client.get("/api/catalog")).json()
        # Порядок здесь — порядок присылки: тасует его витрина, а не сервер.
        self.assertEqual([трек["bpm"] for трек in каталог["music"]], [73.5, 174.0])
        self.assertEqual(каталог["music"][0]["flip_ms"], 3265)

        песня = await self.client.get(каталог["music"][1]["url"])
        self.assertEqual(песня.status, 200)
        # Имена файлов повторяются от плейлиста к плейлисту, поэтому кэш
        # держится на метке версии.
        self.assertEqual(песня.headers["Cache-Control"], "public, max-age=604800")
        self.assertIn("?v=", каталог["music"][1]["url"])

    async def test_фотографии_кэшируются_надолго(self) -> None:
        config.PHOTOS.mkdir(parents=True, exist_ok=True)
        (config.PHOTOS / "снимок.jpg").write_bytes(b"\xff\xd8\xff")
        response = await self.client.get("/photos/снимок.jpg")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "public, max-age=604800")


class ТестыЛимитаЗаявок(unittest.TestCase):
    def test_память_лимита_не_растёт_бесконечно(self) -> None:
        server._recent_orders.clear()
        давно = time.time() - server.ORDER_WINDOW - 1
        for user_id in range(server.ORDER_MEMORY + 1):
            server._recent_orders[user_id] = [давно]
        server._recent_orders[7] = [time.time()]  # этот оформлял заявку только что
        try:
            self.assertFalse(server._rate_limited(999999))
            # Забытые окна выметены, живые попытки остались.
            self.assertEqual(sorted(server._recent_orders), [7, 999999])
        finally:
            server._recent_orders.clear()

if __name__ == "__main__":
    unittest.main()
