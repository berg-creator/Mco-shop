"""Админка в боте: мастер добавления товара, остатки, скрыть и удалить.

Мастер — это состояние, размазанное по семи сообщениям, и ломается оно тихо:
пропущенный шаг, «отмена» посреди разговора, фото, которое не скачалось.
Поэтому здесь проверяется каждый шаг и каждый выход из него, а заодно то,
ради чего админка вообще есть: остатки и возврат распроданной вещи в витрину.
"""

from __future__ import annotations

import unittest
from unittest import mock

from aiogram.dispatcher.event.bases import UNHANDLED

from src import admin, bot, config, db, music
from tests.fakes import ФейкБот, ФейкКнопка, ФейкСообщение, ФейкТрек, ФейкФайл, состояние
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
        self.assertIn(bot.BTN_ADD, message.последний_ответ)

    async def test_поиск_по_номеру_достаёт_старый_товар(self) -> None:
        # Не «последние двадцать»: до вещи из начала каталога иначе не добраться.
        product_id = self.товар()
        message = ФейкСообщение(f"/items {product_id}")
        await admin.items(message, настройки())
        self.assertIn("Нашёл 1", message.answers[0][0])
        self.assertIn(f"№{product_id}", message.последний_ответ)

    async def test_поиск_по_названию_не_смотрит_на_регистр(self) -> None:
        self.товар(name="Куртка")
        message = ФейкСообщение("/items куртка")
        await admin.items(message, настройки())
        self.assertIn("Куртка", message.последний_ответ)

    async def test_поиск_по_номеру_мимо(self) -> None:
        message = ФейкСообщение("/items 999")
        await admin.items(message, настройки())
        self.assertIn("Ничего не нашлось", message.последний_ответ)

    async def test_поиск_по_названию_мимо(self) -> None:
        self.товар()
        message = ФейкСообщение("/items шапка")
        await admin.items(message, настройки())
        self.assertIn("Ничего не нашлось", message.последний_ответ)

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


class МузыкаВитрины(БазаНаВремя, unittest.IsolatedAsyncioTestCase):
    """`/music`: плейлист магазина и ритм листания под то, что играет.

    Темп здесь не меряется по-настоящему — заглушка присылает три байта,
    и ffmpeg на них честно спотыкается. Проверяется другое: что песни
    доезжают до витрины пачкой, что неудачный замер их не выбрасывает
    и что «-» убирает музыку целиком.
    """

    async def asyncSetUp(self) -> None:
        self.state = состояние()
        self.bot = ФейкБот()

    async def прислать(self, track: ФейкТрек, bot: ФейкБот | None = None) -> ФейкСообщение:
        message = ФейкСообщение(audio=track)
        await admin.music_file(message, self.state, bot or self.bot)
        return message

    async def test_альбом_приезжает_пачкой_и_задаёт_ритм(self) -> None:
        await admin.music_start(ФейкСообщение("/music"), self.state)
        self.assertEqual(await self.state.get_state(), admin.NewMusic.file)

        с_темпом = {"bpm": 174.0, "flip_ms": 2758, "offset_ms": 250}
        with mock.patch.object(music, "analyze", return_value=с_темпом):
            первая = await self.прислать(ФейкТрек(title="Renegade Snares"))
            # Вторая песня приходит следом, без «Музыки» перед каждым файлом.
            вторая = await self.прислать(ФейкТрек(file_name="Nu Birth of Cool.mp3"))

        self.assertIn("174.0 BPM", первая.последний_ответ)
        self.assertIn("в плейлисте 2", вторая.последний_ответ)
        плейлист = music.playlist()
        self.assertEqual([трек["title"] for трек in плейлист], ["Renegade Snares", "Nu Birth of Cool"])
        self.assertEqual(плейлист[0]["flip_ms"], 2758)
        # Имена файлов разные: одновременная закачка не затирает соседа.
        self.assertTrue(плейлист[0]["url"].startswith("/music/track-1.mp3?v="))
        self.assertTrue(плейлист[1]["url"].startswith("/music/track-2.mp3?v="))
        # Со второго раза бот показывает, что уже лежит в плейлисте.
        снова = ФейкСообщение("/music")
        await admin.music_start(снова, self.state)
        self.assertIn("Renegade Snares", снова.последний_ответ)

    async def test_без_музыки_бот_говорит_что_тихо(self) -> None:
        message = ФейкСообщение("/music")
        await admin.music_start(message, self.state)
        self.assertIn("тихо", message.последний_ответ)

    async def test_неизмеренный_темп_не_повод_отказать(self) -> None:
        message = await self.прислать(ФейкТрек())
        self.assertIn("Темп определить не вышло", message.последний_ответ)
        плейлист = music.playlist()
        self.assertEqual(плейлист[0]["flip_ms"], music.DEFAULT_FLIP["flip_ms"])
        # Ни тегов, ни имени файла — песня всё равно называется как-то.
        self.assertEqual(плейлист[0]["title"], "Без названия")

    async def test_слишком_большой_файл_не_молчит(self) -> None:
        message = await self.прислать(ФейкТрек(file_size=admin.MAX_TRACK + 1))
        self.assertIn("МБ", message.последний_ответ)
        self.assertEqual(self.bot.downloaded, [])

    async def test_несостоявшаяся_загрузка_видна(self) -> None:
        message = await self.прислать(ФейкТрек(), bot=ФейкБот(падает=True))
        self.assertIn("не скачался", message.последний_ответ)
        self.assertEqual(music.playlist(), [])
        # Занятое под закачку имя освобождается: следующая песня берёт его.
        self.assertEqual(music.free_name(".mp3"), "track-1.mp3")

    async def test_прочерк_убирает_музыку(self) -> None:
        await self.прислать(ФейкТрек())
        await admin.music_start(ФейкСообщение("/music"), self.state)
        message = ФейкСообщение("-")
        await admin.music_text(message, self.state)
        self.assertIn("тихо", message.последний_ответ)
        self.assertEqual(music.playlist(), [])

    async def test_отмена_выходит_из_мастера(self) -> None:
        await admin.music_start(ФейкСообщение("/music"), self.state)
        message = ФейкСообщение("отмена")
        await admin.music_text(message, self.state)
        self.assertEqual(message.последний_ответ, "Отменил.")
        self.assertIsNone(await self.state.get_state())

    async def test_на_болтовню_бот_всё_ещё_ждёт_файл(self) -> None:
        await admin.music_start(ФейкСообщение("/music"), self.state)
        message = ФейкСообщение("а можно ссылкой")
        await admin.music_text(message, self.state)
        self.assertIn("Жду песню файлом", message.последний_ответ)
        self.assertEqual(await self.state.get_state(), admin.NewMusic.file)


if __name__ == "__main__":
    unittest.main()


class КомандаПротивМастера(БазаНаВремя, unittest.IsolatedAsyncioTestCase):
    """Здесь проверяется не обработчик, а порядок: кому достанется сообщение.

    Мастер ловит любой текст, поэтому брошенный на полпути «Остаток» съедал
    следующую команду: `/start` становился размером товара, а магазин
    не открывался. Зовём тот же наблюдатель, что и aiogram, — иначе
    проверялась бы функция, а не то, что событие уходит мимо неё.
    """

    async def прогон(self, text: str, шаг) -> tuple:
        state = состояние()
        await state.set_state(шаг)
        message = ФейкСообщение(text)
        итог = await admin.router.message.trigger(
            message,
            state=state,
            raw_state=await state.get_state(),
            settings=настройки(),
            bot=ФейкБот(),
        )
        return итог, message, state

    async def test_команда_посреди_мастера_уходит_дальше(self) -> None:
        итог, message, state = await self.прогон("/start", admin.EditStock.value)
        self.assertIs(итог, UNHANDLED)  # событие досталось магазину
        self.assertIsNone(await state.get_state())  # мастер закрыт, а не ждёт ответа
        self.assertEqual(message.answers, [])  # размером товара «/START» не стал

    async def test_кнопка_посреди_мастера_работает_как_команда(self) -> None:
        # Кнопка шлёт текст без «/», и без своего условия «📦 Товары» стали бы
        # остатком товара, а список так и не открылся бы.
        итог, message, state = await self.прогон(bot.BTN_ITEMS, admin.EditStock.value)
        self.assertIsNot(итог, UNHANDLED)
        self.assertIsNone(await state.get_state())
        # Список, а не поиск по слову «Товары»: у кнопки запроса нет.
        self.assertIn(bot.BTN_ADD, message.последний_ответ)

    async def test_обычный_ответ_остаётся_в_мастере(self) -> None:
        итог, message, state = await self.прогон("M:2", admin.EditStock.value)
        self.assertIsNot(итог, UNHANDLED)
        self.assertEqual(message.последний_ответ, "Товар уже удалён.")
