"""Обработчики бота: заявка владельцу, кнопки «Подтвердить»/«Отказать», /orders.

Проверяется то, что происходит между базой и Telegram: кто имеет право нажать
кнопку, что увидит покупатель, и что заявка не теряется, когда Telegram
отказался доставить сообщение или переписать его.

    python -m unittest discover tests
"""

from __future__ import annotations

import unittest

from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import MenuButtonWebApp, WebAppInfo

from src import bot, db
from tests.fakes import (  # noqa: F401
    ФейкБот,
    ФейкКнопка,
    ФейкПересылка,
    ФейкПользователь,
    ФейкСообщение,
    ФейкФайл,
)
from tests.test_shop import БазаНаВремя, настройки


class ТестыКоманд(БазаНаВремя, unittest.IsolatedAsyncioTestCase):
    async def test_старт_показывает_кнопку_витрины(self) -> None:
        message = ФейкСообщение("/start", user_id=77)
        await bot.start(message, настройки())
        text, keyboard = message.answers[0]
        self.assertIn("Mco shop", text)
        self.assertEqual(
            keyboard.keyboard[0][0].web_app.url, "https://example.org/app/"
        )

    async def test_покупателю_не_показываются_кнопки_владельца(self) -> None:
        message = ФейкСообщение("/start", user_id=77)
        await bot.start(message, настройки())
        подписи = [кнопка.text for ряд in message.answers[0][1].keyboard for кнопка in ряд]
        self.assertEqual(подписи, [bot.BTN_SHOP, bot.BTN_HELP])

    async def test_владельцу_команды_приходят_кнопками(self) -> None:
        message = ФейкСообщение("/start", user_id=1)
        await bot.start(message, настройки())
        подписи = [кнопка.text for ряд in message.answers[0][1].keyboard for кнопка in ряд]
        self.assertEqual(set(bot.ADMIN_BUTTONS) - set(подписи), set())

    async def test_кнопка_как_заказать_работает_как_команда(self) -> None:
        # Проверяется фильтр, а не функция: кнопка шлёт обычный текст, и без
        # своего условия он ушёл бы владельцу как вопрос покупателя.
        message = ФейкСообщение(bot.BTN_HELP, user_id=77)
        await bot.router.message.trigger(message, settings=настройки(), bot=ФейкБот())
        self.assertIn("Как это работает", message.последний_ответ)
        self.assertEqual(message.forwards, [])

    async def test_shop_ведёт_в_витрину(self) -> None:
        message = ФейкСообщение("/shop")
        await bot.open_shop(message, настройки())
        self.assertIn("Витрина", message.последний_ответ)

    async def test_help_покупателю_без_команд_владельца(self) -> None:
        message = ФейкСообщение("/help", user_id=77)
        await bot.help_command(message, настройки())
        self.assertNotIn("/add", message.последний_ответ)

    async def test_help_владельцу_с_командами_владельца(self) -> None:
        message = ФейкСообщение("/help", user_id=1)
        await bot.help_command(message, настройки())
        self.assertIn("/items", message.последний_ответ)

    async def test_кнопка_меню_ставится_на_адрес_витрины(self) -> None:
        fake = ФейкБот()
        await bot.setup_bot_menu(fake, настройки())
        self.assertEqual(fake.menu_button.web_app.url, "https://example.org/app/")

    async def test_команды_покупателю_короче_чем_владельцу(self) -> None:
        fake = ФейкБот()
        await bot.setup_bot_menu(fake, настройки())
        (общий, покупателю), (личный, владельцу) = fake.commands
        self.assertIsNone(общий)
        self.assertEqual(покупателю, ["start", "help"])
        self.assertEqual(личный.chat_id, 1)
        self.assertIn("add", владельцу)

    async def test_смена_адреса_витрины_шлёт_владельцу_свежую_кнопку(self) -> None:
        """Кнопка под полем ввода живёт в чате: без нового сообщения её не сменить."""
        fake = ФейкБот()
        fake.menu_button = MenuButtonWebApp(
            text="Магазин", web_app=WebAppInfo(url="https://старый-туннель.example/app/")
        )
        await bot.setup_bot_menu(fake, настройки())
        self.assertEqual([chat_id for chat_id, _ in fake.sent], [1])

    async def test_прежний_адрес_витрины_владельца_не_будит(self) -> None:
        fake = ФейкБот()
        fake.menu_button = MenuButtonWebApp(
            text="Магазин", web_app=WebAppInfo(url="https://example.org/app/")
        )
        await bot.setup_bot_menu(fake, настройки())
        self.assertEqual(fake.sent, [])

    async def test_молчащий_чат_владельца_не_мешает_запуску(self) -> None:
        fake = ФейкБот(падает=True)
        with self.assertLogs("src.bot", level="WARNING"):
            await bot.setup_bot_menu(fake, настройки())
        self.assertEqual(len(fake.commands), 1)


class ЗаявкаВБоте(БазаНаВремя):
    """Общая заготовка: товар, заявка, ответ владельца."""

    def товар(self, **kwargs) -> dict:
        поля = {"name": "Куртка", "brand": "Stone Island", "price": 42000, "sizes": {"M": 1}}
        поля.update(kwargs)
        return db.get_product(db.add_product(**поля))

    def заявка(self, product: dict | None = None, quantity: int = 1, **kwargs) -> dict:
        product = product or self.товар()
        поля = {
            "user_id": 500, "username": "buyer", "customer_name": "Вася",
            "phone": "+79001234567", "delivery": "", "address": "", "comment": "",
        }
        поля.update(kwargs)
        order = db.create_order(
            items=[{"variant_id": product["sizes"][0]["variant_id"], "quantity": quantity}],
            **поля,
        )
        return db.get_order(order["id"])


class ТестыТекстаЗаявки(ЗаявкаВБоте):
    def test_короткая_заявка_без_необязательных_полей(self) -> None:
        text = bot.order_text(self.заявка(username=""), настройки())
        self.assertIn("Покупатель: Вася", text)
        self.assertNotIn("@", text)
        self.assertNotIn("Доставка:", text)
        self.assertNotIn("Адрес:", text)
        self.assertNotIn("Комментарий:", text)
        self.assertNotIn("Статус:", text)  # новая заявка статусом не подписывается

    def test_полная_заявка_со_всеми_полями(self) -> None:
        order = self.заявка(
            delivery="СДЭК", address="Москва, Тверская 1", comment="позвоните вечером"
        )
        text = bot.order_text(order, настройки())
        self.assertIn("(@buyer)", text)
        self.assertIn("Доставка: СДЭК", text)
        self.assertIn("Адрес: Москва, Тверская 1", text)
        self.assertIn("Комментарий: позвоните вечером", text)
        self.assertIn("42 000 ₽", text)  # тысячи разделяются пробелом, не запятой

    def test_безымянный_покупатель_не_оставляет_пустое_место(self) -> None:
        text = bot.order_text(self.заявка(customer_name=""), настройки())
        self.assertIn("Покупатель: без имени", text)

    def test_вещь_без_размера_и_несколько_штук(self) -> None:
        product = self.товар(name="Шапка", sizes={db.ONE_SIZE: 3})
        text = bot.order_text(self.заявка(product, quantity=2), настройки())
        self.assertNotIn("размер", text)
        self.assertIn("2 шт", text)

    def test_закрытая_заявка_подписана_статусом(self) -> None:
        order = self.заявка()
        db.set_order_status(order["id"], "confirmed")
        text = bot.order_text(db.get_order(order["id"]), настройки())
        self.assertIn("Статус: подтверждена", text)

    def test_протухшая_заявка_объясняет_себя(self) -> None:
        order = self.заявка()
        with db.connect() as conn:
            conn.execute("UPDATE orders SET status = 'expired' WHERE id = ?", (order["id"],))
        text = bot.order_text(db.get_order(order["id"]), настройки())
        self.assertIn("вещи вернулись в витрину", text)

    def test_неизвестный_статус_показывается_как_есть(self) -> None:
        order = self.заявка()
        with db.connect() as conn:
            conn.execute("UPDATE orders SET status = 'странный' WHERE id = ?", (order["id"],))
        text = bot.order_text(db.get_order(order["id"]), настройки())
        self.assertIn("Статус: странный", text)


class ТестыУведомления(ЗаявкаВБоте, unittest.IsolatedAsyncioTestCase):
    async def test_заявка_уходит_владельцу_с_кнопками(self) -> None:
        order = self.заявка()
        fake = ФейкБот()
        await bot.notify_admins(fake, настройки(), order["id"])
        chat_id, text = fake.sent[0]
        self.assertEqual(chat_id, 1)
        self.assertIn(f"Заявка №{order['id']}", text)

    async def test_несуществующая_заявка_не_шлётся(self) -> None:
        fake = ФейкБот()
        await bot.notify_admins(fake, настройки(), 999)
        self.assertEqual(fake.sent, [])

    async def test_недоступный_владелец_не_роняет_заявку(self) -> None:
        order = self.заявка()
        with self.assertLogs("src.bot", level="WARNING") as логи:
            await bot.notify_admins(ФейкБот(падает=True), настройки(), order["id"])
        self.assertIn("не дошла", логи.output[0])


class ТестыРешенияПоЗаявке(ЗаявкаВБоте, unittest.IsolatedAsyncioTestCase):
    async def test_подтверждение_списывает_и_пишет_покупателю(self) -> None:
        order = self.заявка()
        fake = ФейкБот()
        callback = ФейкКнопка(f"order:confirm:{order['id']}", user_id=1)
        await bot.decide_order(callback, fake, настройки())

        self.assertEqual(db.get_order(order["id"])["status"], "confirmed")
        self.assertEqual(callback.последний_ответ, "Подтверждено")
        self.assertIn("Статус: подтверждена", callback.message.edits[0][0])
        self.assertEqual(fake.sent[0][0], 500)
        self.assertIn("подтверждена", fake.sent[0][1])

    async def test_отказ_возвращает_вещь_и_зовёт_обратно_в_витрину(self) -> None:
        order = self.заявка()
        fake = ФейкБот()
        callback = ФейкКнопка(f"order:reject:{order['id']}", user_id=1)
        await bot.decide_order(callback, fake, настройки())

        self.assertEqual(db.get_order(order["id"])["status"], "rejected")
        self.assertEqual(callback.последний_ответ, "Отклонено")
        self.assertIn("не сложилось", fake.sent[0][1])
        self.assertEqual(len(db.catalog()["products"]), 1)

    async def test_чужие_руки_к_кнопкам_не_допускаются(self) -> None:
        order = self.заявка()
        callback = ФейкКнопка(f"order:confirm:{order['id']}", user_id=666)
        await bot.decide_order(callback, ФейкБот(), настройки())
        self.assertEqual(callback.answers, [("Это кнопки владельца магазина", True)])
        self.assertEqual(db.get_order(order["id"])["status"], "new")

    async def test_второе_нажатие_показывает_текущее_состояние(self) -> None:
        order = self.заявка()
        db.set_order_status(order["id"], "confirmed")
        callback = ФейкКнопка(f"order:reject:{order['id']}", user_id=1)
        await bot.decide_order(callback, ФейкБот(), настройки())

        self.assertEqual(callback.answers, [("Заявка уже закрыта", True)])
        self.assertIn("Статус: подтверждена", callback.message.edits[0][0])
        self.assertEqual(db.get_order(order["id"])["status"], "confirmed")

    async def test_нажатие_по_исчезнувшей_заявке_не_роняет_бота(self) -> None:
        callback = ФейкКнопка("order:confirm:999", user_id=1)
        await bot.decide_order(callback, ФейкБот(), настройки())
        self.assertEqual(callback.answers, [("Заявка уже закрыта", True)])
        self.assertEqual(callback.message.edits, [])

    async def test_нередактируемое_сообщение_не_отменяет_решения(self) -> None:
        order = self.заявка()
        callback = ФейкКнопка(
            f"order:confirm:{order['id']}", user_id=1,
            message=ФейкСообщение(edit_fails=True),
        )
        with self.assertLogs("src.bot", level="INFO"):
            await bot.decide_order(callback, ФейкБот(), настройки())
        self.assertEqual(db.get_order(order["id"])["status"], "confirmed")

    async def test_покупатель_заблокировал_бота_а_заявка_всё_равно_закрыта(self) -> None:
        order = self.заявка()
        callback = ФейкКнопка(f"order:confirm:{order['id']}", user_id=1)
        with self.assertLogs("src.bot", level="INFO") as логи:
            await bot.decide_order(callback, ФейкБот(падает=True), настройки())
        self.assertEqual(db.get_order(order["id"])["status"], "confirmed")
        self.assertIn("не смог написать покупателю", "\n".join(логи.output))


class ТестыСпискаЗаявок(ЗаявкаВБоте, unittest.IsolatedAsyncioTestCase):
    async def test_чужому_список_заявок_не_показывается(self) -> None:
        self.заявка()
        message = ФейкСообщение("/orders", user_id=666)
        await bot.recent_orders(message, настройки())
        self.assertEqual(message.answers, [])

    async def test_пустой_список_говорит_об_этом(self) -> None:
        message = ФейкСообщение("/orders", user_id=1)
        await bot.recent_orders(message, настройки())
        self.assertEqual(message.последний_ответ, "Заявок пока не было.")

    async def test_у_новой_заявки_кнопки_есть_у_закрытой_нет(self) -> None:
        свежая = self.заявка()
        закрытая = self.заявка(self.товар(name="Худи"))
        db.set_order_status(закрытая["id"], "rejected")

        message = ФейкСообщение("/orders", user_id=1)
        await bot.recent_orders(message, настройки())

        клавиатуры = {
            text.splitlines()[0]: keyboard for text, keyboard in message.answers
        }
        новая_строка = next(k for k in клавиатуры if f"№{свежая['id']}" in k)
        закрытая_строка = next(k for k in клавиатуры if f"№{закрытая['id']}" in k)
        self.assertIsNotNone(клавиатуры[новая_строка])
        self.assertIsNone(клавиатуры[закрытая_строка])


class ТестыВопросов(БазаНаВремя, unittest.IsolatedAsyncioTestCase):
    """Почта между покупателем и владельцем: вопрос туда, ответ обратно."""

    async def test_вопрос_покупателя_уходит_владельцу_с_квитанцией(self) -> None:
        message = ФейкСообщение("Есть этот размер?", user_id=77)
        await bot.ask_seller(message, настройки())
        self.assertEqual(message.forwards, [1])
        self.assertIn("Передал владельцу", message.последний_ответ)

    async def test_чужая_команда_не_беспокоит_владельца(self) -> None:
        # Подобранная на слух команда — это не вопрос покупателя: владельцу
        # прилетало бы «/add» от незнакомого человека, и ответить на это нечем.
        message = ФейкСообщение("/add", user_id=77)
        await bot.ask_seller(message, настройки())
        self.assertEqual(message.forwards, [])
        self.assertEqual(message.answers, [])

    async def test_владелец_сам_себе_вопросов_не_шлёт(self) -> None:
        message = ФейкСообщение("заметка", user_id=1)
        await bot.ask_seller(message, настройки())
        self.assertEqual(message.forwards, [])
        self.assertEqual(message.answers, [])

    async def test_недоступный_владелец_не_обещает_покупателю_лишнего(self) -> None:
        message = ФейкСообщение("Есть этот размер?", user_id=77, падает=True)
        with self.assertLogs("src.bot", level="WARNING"):
            await bot.ask_seller(message, настройки())
        self.assertIn("Не получилось", message.последний_ответ)

    async def test_ответ_владельца_доходит_покупателю(self) -> None:
        вопрос = ФейкСообщение("Есть этот размер?", forward_origin=ФейкПересылка(ФейкПользователь(77)))
        ответ = ФейкСообщение("Да, последний", user_id=1, reply_to=вопрос)
        await bot.answer_customer(ответ, настройки())
        self.assertEqual(ответ.copies, [77])
        self.assertIn("Отправил", ответ.последний_ответ)

    async def test_ответ_покупателя_на_пересылку_идёт_как_вопрос(self) -> None:
        пересланное = ФейкСообщение("чужой пост", forward_origin=ФейкПересылка(ФейкПользователь(9)))
        message = ФейкСообщение("а такое есть?", user_id=77, reply_to=пересланное)
        with self.assertRaises(SkipHandler):
            await bot.answer_customer(message, настройки())

    async def test_скрытая_пересылка_не_даёт_ответить_молча(self) -> None:
        вопрос = ФейкСообщение("Есть этот размер?", forward_origin=ФейкПересылка())
        ответ = ФейкСообщение("Да", user_id=1, reply_to=вопрос)
        await bot.answer_customer(ответ, настройки())
        self.assertEqual(ответ.copies, [])
        self.assertIn("скрыл пересылку", ответ.последний_ответ)

    async def test_заблокировавший_бота_покупатель_виден_владельцу(self) -> None:
        вопрос = ФейкСообщение("Есть этот размер?", forward_origin=ФейкПересылка(ФейкПользователь(77)))
        ответ = ФейкСообщение("Да", user_id=1, reply_to=вопрос, падает=True)
        with self.assertLogs("src.bot", level="INFO"):
            await bot.answer_customer(ответ, настройки())
        self.assertIn("Не доставил", ответ.последний_ответ)


if __name__ == "__main__":
    unittest.main()
