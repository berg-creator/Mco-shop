"""Тесты на то, что стоит денег и репутации: остатки, подпись, разбор постов.

Витрина и тексты бота проверяются глазами, а вот эти четыре вещи глазами
не проверишь: двойная продажа последней вещи, подделанная подпись, цена,
угаданная из поста неправильно, и резерв, который не вернулся после отказа.

    python -m unittest discover tests
"""

from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path

from src import config, db, importer, server

TOKEN = "123456:TESTTOKEN"


def signed_init_data(user_id: int = 42, age_seconds: int = 0) -> str:
    """Собирает initData так же, как это делает Telegram."""
    pairs = {
        "auth_date": str(int(time.time()) - age_seconds),
        "query_id": "AAH",
        "user": json.dumps({"id": user_id, "first_name": "Тест"}, ensure_ascii=False),
    }
    check = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(pairs)


class БазаНаВремя(unittest.TestCase):
    """Каждый тест работает со своей базой во временной папке."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self._db_file = config.DB_FILE
        config.DB_FILE = Path(self._temp.name) / "shop.db"
        db.init()

    def tearDown(self) -> None:
        config.DB_FILE = self._db_file
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


if __name__ == "__main__":
    unittest.main()
