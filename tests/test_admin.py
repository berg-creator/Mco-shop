"""Админка в боте: мастер добавления товара, остатки, скрыть и удалить.

Мастер — это состояние, размазанное по семи сообщениям, и ломается оно тихо:
пропущенный шаг, «отмена» посреди разговора, фото, которое не скачалось.
Поэтому здесь проверяется каждый шаг и каждый выход из него, а заодно то,
ради чего админка вообще есть: остатки и возврат распроданной вещи в витрину.
"""

from __future__ import annotations

import unittest

from src import admin, config, db
from tests.fakes import ФейкБот, ФейкКнопка, ФейкСообщение, ФейкФайл, состояние
from tests.test_shop import БазаНаВремя, настройки


class ТестыДопуска(БазаНаВремя, unittest.IsolatedAsyncioTestCase):
    async def test_владелец_проходит(self) -> None:
        self.assertTrue(await admin.IsAdmin()(ФейкСообщение(user_id=1), настройки()))

    async def test_чужой_не_проходит(self) -> None:
        self.assertFalse(await admin.IsAdmin()(ФейкСообщение(user_id=666), настройки()))

    async def test_событие_без_отправителя_не_проходит(self) -> None:
        class БезОтправителя:
            pass

        self.assertFalse(await admin.IsAdmin()(БезОтправителя(), настройки()))


class ТестыРазбораРазмеров(unittest.TestCase):
    def test_прочерк_значит_вещь_без_размера(self) -> None:
        self.assertEqual(admin.parse_sizes("-"), {db.ONE_SIZE: 1})

    def test_размеры_с_количеством_и_без(self) -> None:
        self.assertEqual(admin.parse_sizes("M:2 L, XL"), {"M": 2, "L": 1, "XL": 1})

    def test_длинный_размер_обрезается(self) -> None:
        self.assertEqual(admin.parse_sizes("оченьдлинный"), {"ОЧЕНЬД": 1})

    def test_мусор_из_разделителей_даёт_вещь_без_размера(self) -> None:
        self.assertEqual(admin.parse_sizes(" : , : "), {db.ONE_SIZE: 1})


class МастерТовара(БазаНаВремя, unittest.IsolatedAsyncioTestCase):
    """Прогон мастера: шаги вызываются так же, как их вызвал бы диспетчер."""

    async def asyncSetUp(self) -> None:
        self.state = состояние()
        self.bot = ФейкБот()

    async def шаг(self, text: str, handler, **kwargs) -> ФейкСообщение:
        message = ФейкСообщение(text)
        await handler(message, self.state, **kwargs)
        return message

    async def test_товар_заводится_за_семь_шагов(self) -> None:
        await self.шаг("/add", admin.add_start)
        self.assertEqual(await self.state.get_state(), admin.NewProduct.photos)

        фото = ФейкСообщение(photo=[ФейкФайл("маленькое"), ФейкФайл("большое")])
        await admin.add_photo(фото, self.state, self.bot)
        self.assertEqual(self.bot.downloaded, ["большое"])
        self.assertIn("Фото принято", фото.последний_ответ)

        второе = ФейкСообщение(photo=[ФейкФайл("ещё")])
        await admin.add_photo(второе, self.state, self.bot)
        self.assertIn("(2)", второе.последний_ответ)

        кнопка = ФейкКнопка("add:photos_done")
        await admin.add_photos_done(кнопка, self.state)
        self.assertEqual(await self.state.get_state(), admin.NewProduct.name)

        await self.шаг("Куртка Ghost", admin.add_name)
        await self.шаг("Stone Island", admin.add_brand)

        категории = {c["name"]: c["id"] for c in db.categories()}
        выбор = ФейкКнопка(f"add:cat:{категории['Куртки']}")
        await admin.add_category(выбор, self.state)

        await self.шаг("24000 из 30000", admin.add_price)
        await self.шаг("M:2 L", admin.add_sizes)
        готово = await self.шаг("Носили сезон, состояние хорошее", admin.add_description,
                                settings=настройки())

        self.assertIn("в витрине", готово.последний_ответ)
        товар = db.catalog()["products"][0]
        self.assertEqual(товар["name"], "Куртка Ghost")
        self.assertEqual(товар["brand"], "Stone Island")
        self.assertEqual(товар["price"], 24000)
        self.assertEqual(товар["old_price"], 30000)
        self.assertEqual(товар["category"], "jackets")
        self.assertEqual(товар["condition"], "used")  # «носили» — это б/у
        self.assertEqual({s["size"]: s["available"] for s in товар["sizes"]}, {"M": 2, "L": 1})
        self.assertEqual(len(товар["photos"]), 2)
        self.assertIsNone(await self.state.get_state())  # мастер закрылся сам

    async def test_товар_без_фото_бренда_категории_и_описания(self) -> None:
        await self.шаг("/add", admin.add_start)
        await self.шаг("-", admin.add_photos_text)
        await self.шаг("Шапка", admin.add_name)
        await self.шаг("-", admin.add_brand)
        await admin.add_category(ФейкКнопка("add:cat:0"), self.state)
        await self.шаг("7000", admin.add_price)
        await self.шаг("-", admin.add_sizes)
        await self.шаг("-", admin.add_description, settings=настройки())

        товар = db.catalog()["products"][0]
        self.assertEqual(товар["brand"], "")
        self.assertIsNone(товар["category"])
        self.assertEqual(товар["description"], "")
        self.assertEqual(товар["condition"], "new")
        self.assertEqual(товар["sizes"][0]["size"], db.ONE_SIZE)

    async def test_больше_пяти_фото_не_принимается(self) -> None:
        await self.шаг("/add", admin.add_start)
        for _ in range(admin.MAX_PHOTOS):
            await admin.add_photo(ФейкСообщение(photo=[ФейкФайл()]), self.state, self.bot)
        лишнее = ФейкСообщение(photo=[ФейкФайл()])
        await admin.add_photo(лишнее, self.state, self.bot)

        self.assertIn("не нужно", лишнее.последний_ответ)
        self.assertEqual(len(self.bot.downloaded), admin.MAX_PHOTOS)

    async def test_несохранившееся_фото_не_молчит(self) -> None:
        await self.шаг("/add", admin.add_start)
        сломанное = ФейкСообщение(photo=[ФейкФайл()])
        with self.assertLogs("src.admin", level="WARNING"):
            await admin.add_photo(сломанное, self.state, ФейкБот(падает=True))
        self.assertIn("не скачалось", сломанное.последний_ответ)
        self.assertEqual((await self.state.get_data())["photos"], [])

    async def test_нулевая_цена_не_принимается(self) -> None:
        await self.шаг("/add", admin.add_start)
        await self.шаг("-", admin.add_photos_text)
        await self.шаг("Куртка", admin.add_name)
        await self.шаг("-", admin.add_brand)
        await admin.add_category(ФейкКнопка("add:cat:0"), self.state)

        for неверная in ("бесплатно", "0"):
            ответ = await self.шаг(неверная, admin.add_price)
            self.assertIn("Не понял цену", ответ.последний_ответ)
            self.assertEqual(await self.state.get_state(), admin.NewProduct.price)

    async def test_вторая_цена_ниже_первой_не_становится_скидкой(self) -> None:
        await self.state.set_state(admin.NewProduct.price)
        await self.шаг("24000 вместо 20000", admin.add_price)
        self.assertIsNone((await self.state.get_data())["old_price"])

    async def test_отмена_работает_на_любом_шаге(self) -> None:
        шаги = [
            (admin.NewProduct.photos, admin.add_photos_text, {}),
            (admin.NewProduct.name, admin.add_name, {}),
            (admin.NewProduct.brand, admin.add_brand, {}),
            (admin.NewProduct.price, admin.add_price, {}),
            (admin.NewProduct.sizes, admin.add_sizes, {}),
            (admin.NewProduct.description, admin.add_description, {"settings": настройки()}),
            (admin.EditStock.value, admin.set_stock, {"settings": настройки()}),
        ]
        for state, handler, kwargs in шаги:
            await self.state.set_state(state)
            ответ = await self.шаг("отмена", handler, **kwargs)
            self.assertEqual(ответ.последний_ответ, "Отменил.", state)
            self.assertIsNone(await self.state.get_state())

    async def test_команда_cancel_выходит_из_мастера(self) -> None:
        await self.шаг("/add", admin.add_start)
        ответ = await self.шаг("/cancel", admin.cancel)
        self.assertEqual(ответ.последний_ответ, "Отменил.")
        self.assertIsNone(await self.state.get_state())


class ТестыСпискаТоваров(БазаНаВремя, unittest.IsolatedAsyncioTestCase):
    def товар(self, **kwargs) -> int:
        поля = {"name": "Куртка", "brand": "Stone Island", "price": 42000, "sizes": {"M": 1}}
        поля.update(kwargs)
        return db.add_product(**поля)

    async def test_пустой_список_зовёт_добавить(self) -> None:
        message = ФейкСообщение("/items")
        await admin.items(message, настройки())
        self.assertIn("/add", message.последний_ответ)

    async def test_список_показывает_остаток_и_статус(self) -> None:
        self.товар()
        message = ФейкСообщение("/items")
        await admin.items(message, настройки())
        строка, клавиатура = message.answers[-1]
        self.assertIn("M×1", строка)
        self.assertIn("в витрине", строка)
        self.assertEqual(len(клавиатура.inline_keyboard[0]), 2)  # остаток и скрыть

    async def test_у_распроданной_вещи_одна_кнопка_вернуть_остаток(self) -> None:
        product_id = self.товар(status="sold", sizes={"M": 0})
        message = ФейкСообщение("/items")
        await admin.items(message, настройки())
        строка, клавиатура = message.answers[-1]
        self.assertIn("нет в наличии", строка)
        self.assertIn("продан", строка)
        self.assertEqual(len(клавиатура.inline_keyboard[0]), 1)
        self.assertEqual(
            клавиатура.inline_keyboard[0][0].callback_data, f"item:stock:{product_id}"
        )

    async def test_скрытый_товар_предлагает_вернуться_в_витрину(self) -> None:
        self.товар(status="hidden")
        message = ФейкСообщение("/items")
        await admin.items(message, настройки())
        _, клавиатура = message.answers[-1]
        self.assertEqual(клавиатура.inline_keyboard[0][1].text, "Вернуть в витрину")

    async def test_неизвестный_статус_показывается_как_есть(self) -> None:
        product_id = self.товар()
        with db.connect() as conn:
            conn.execute("UPDATE products SET status = 'странный' WHERE id = ?", (product_id,))
        строка = admin._product_line(db.get_product(product_id), настройки())
        self.assertIn("странный", строка)


class ТестыКнопокТовара(БазаНаВремя, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.product_id = db.add_product(
            name="Куртка", brand="Stone Island", price=42000, sizes={"M": 1}
        )

    async def test_скрыть_и_вернуть_в_витрину(self) -> None:
        кнопка = ФейкКнопка(f"item:hide:{self.product_id}")
        await admin.toggle_hidden(кнопка, настройки())
        self.assertEqual(db.get_product(self.product_id)["status"], "hidden")
        self.assertEqual(кнопка.последний_ответ, "Скрыт")
        self.assertIn("скрыт", кнопка.message.edits[-1][0])

        await admin.toggle_hidden(кнопка, настройки())
        self.assertEqual(db.get_product(self.product_id)["status"], "active")
        self.assertEqual(кнопка.последний_ответ, "В витрине")

    async def test_удалённый_товар_не_прячется(self) -> None:
        db.delete_product(self.product_id)
        кнопка = ФейкКнопка(f"item:hide:{self.product_id}")
        await admin.toggle_hidden(кнопка, настройки())
        self.assertEqual(кнопка.answers, [("Товар уже удалён", True)])

    async def test_распроданное_нельзя_вернуть_в_витрину_без_остатка(self) -> None:
        db.set_quantity(self.product_id, "M", 0)
        db.update_product(self.product_id, status="sold")
        кнопка = ФейкКнопка(f"item:hide:{self.product_id}")
        await admin.toggle_hidden(кнопка, настройки())
        self.assertEqual(кнопка.answers, [("Вещь распродана — сначала верни остаток", True)])
        self.assertEqual(db.get_product(self.product_id)["status"], "sold")

    async def test_удаление_убирает_товар_из_базы(self) -> None:
        кнопка = ФейкКнопка(f"item:delete:{self.product_id}")
        await admin.delete_item(кнопка)
        self.assertIsNone(db.get_product(self.product_id))
        self.assertEqual(кнопка.последний_ответ, "Удалено")
        self.assertIn("удалён", кнопка.message.edits[-1][0])


class ТестыОстатковИзАдминки(БазаНаВремя, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.state = состояние()
        self.product_id = db.add_product(
            name="Куртка", brand="Stone Island", price=42000, sizes={"M": 1, "L": 1}
        )

    async def спросить_остаток(self) -> None:
        await admin.ask_stock(ФейкКнопка(f"item:stock:{self.product_id}"), self.state)

    async def test_новый_остаток_обнуляет_исчезнувшие_размеры(self) -> None:
        await self.спросить_остаток()
        self.assertEqual(await self.state.get_state(), admin.EditStock.value)

        message = ФейкСообщение("M:3")
        await admin.set_stock(message, self.state, настройки())

        размеры = {s["size"]: s["available"] for s in db.get_product(self.product_id)["sizes"]}
        self.assertEqual(размеры, {"M": 3, "L": 0})
        self.assertIn("M×3", message.последний_ответ)

    async def test_остаток_возвращает_распроданную_вещь_в_витрину(self) -> None:
        db.set_quantity(self.product_id, "M", 0)
        db.set_quantity(self.product_id, "L", 0)
        db.update_product(self.product_id, status="sold")

        await self.спросить_остаток()
        await admin.set_stock(ФейкСообщение("M:1"), self.state, настройки())

        self.assertEqual(db.get_product(self.product_id)["status"], "active")
        self.assertEqual(len(db.catalog()["products"]), 1)

    async def test_нулевой_остаток_не_возвращает_вещь_в_витрину(self) -> None:
        db.set_quantity(self.product_id, "M", 0)
        db.set_quantity(self.product_id, "L", 0)
        db.update_product(self.product_id, status="sold")

        await self.спросить_остаток()
        await admin.set_stock(ФейкСообщение("M:0"), self.state, настройки())
        self.assertEqual(db.get_product(self.product_id)["status"], "sold")

    async def test_остаток_для_удалённого_товара_не_роняет_бота(self) -> None:
        await self.спросить_остаток()
        db.delete_product(self.product_id)
        message = ФейкСообщение("M:1")
        await admin.set_stock(message, self.state, настройки())
        self.assertEqual(message.последний_ответ, "Товар уже удалён.")


if __name__ == "__main__":
    unittest.main()
