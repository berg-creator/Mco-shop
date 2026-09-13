"""Настройки, запуск и командная строка базы.

Это код, который выполняется ровно один раз — на старте, — и поэтому ломается
незаметно: опечатка в `.env`, порт строкой, забытый `https://`. Проверяется
каждая ветка разбора настроек и каждый режим запуска, включая тот, в котором
магазин отказывается стартовать и должен внятно сказать почему.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src import app, config, db, server
from tests.test_shop import БазаНаВремя, настройки

ОКРУЖЕНИЕ = (
    "TELEGRAM_BOT_TOKEN ADMIN_IDS WEBAPP_URL SHOP_NAME PORT "
    "DELIVERY_OPTIONS CURRENCY DEV_ALLOW_UNSIGNED"
).split()


class НастройкиНаВремя(unittest.TestCase):
    """Настройки читаются из своей папки: рабочий `.env` в тесты не лезет."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self._root = config.ROOT
        config.ROOT = Path(self._temp.name)
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        for key in ОКРУЖЕНИЕ:
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        self._env.stop()
        config.ROOT = self._root
        self._temp.cleanup()

    def записать_env(self, text: str) -> None:
        (config.ROOT / ".env").write_text(text, encoding="utf-8")


class ТестыРазбораEnv(НастройкиНаВремя):
    def test_отсутствующий_файл_это_пустые_настройки(self) -> None:
        self.assertEqual(config._read_env_file(config.ROOT / "нет.env"), {})

    def test_комментарии_пустые_строки_и_кавычки(self) -> None:
        self.записать_env(
            "# комментарий\n"
            "\n"
            "строка без равенства\n"
            "SHOP_NAME = 'Mco shop'\n"
            'CURRENCY="$"\n'
        )
        значения = config._read_env_file(config.ROOT / ".env")
        self.assertEqual(значения, {"SHOP_NAME": "Mco shop", "CURRENCY": "$"})


class ТестыЗагрузкиНастроек(НастройкиНаВремя):
    def test_полные_настройки_из_файла(self) -> None:
        self.записать_env(
            "TELEGRAM_BOT_TOKEN=123:ABC\n"
            "ADMIN_IDS=1, 2\n"
            "WEBAPP_URL=https://example.org/app/\n"
            "SHOP_NAME=Лавка\n"
            "PORT=9090\n"
            "DELIVERY_OPTIONS=Самовывоз; СДЭК ;\n"
            "CURRENCY=₽\n"
            "DEV_ALLOW_UNSIGNED=yes\n"
        )
        settings = config.load()
        self.assertEqual(settings.problems, ())
        self.assertTrue(settings.ready)
        self.assertEqual(settings.admin_ids, (1, 2))
        self.assertEqual(settings.port, 9090)
        self.assertEqual(settings.delivery_options, ("Самовывоз", "СДЭК"))
        self.assertTrue(settings.dev_allow_unsigned)
        self.assertTrue(settings.is_admin(2))
        self.assertFalse(settings.is_admin(3))

    def test_переменная_окружения_главнее_файла(self) -> None:
        self.записать_env("SHOP_NAME=Из файла\n")
        os.environ["SHOP_NAME"] = "Из окружения"
        self.assertEqual(config.load().shop_name, "Из окружения")

    def test_пустые_настройки_собирают_все_претензии(self) -> None:
        settings = config.load()
        проблемы = "\n".join(settings.problems)
        self.assertFalse(settings.ready)
        self.assertIn("TELEGRAM_BOT_TOKEN", проблемы)
        self.assertIn("ADMIN_IDS пуст", проблемы)
        self.assertIn("WEBAPP_URL пуст", проблемы)
        self.assertEqual(settings.port, 8080)  # значения по умолчанию всё равно на месте
        self.assertEqual(settings.shop_name, "Mco shop")

    def test_нечисловой_админ_называется_поимённо(self) -> None:
        self.записать_env("ADMIN_IDS=1,вася\n")
        self.assertIn("«вася» не похож", "\n".join(config.load().problems))

    def test_http_адрес_витрины_это_проблема(self) -> None:
        self.записать_env("WEBAPP_URL=http://example.org/app/\n")
        self.assertIn("не пускает в http", "\n".join(config.load().problems))

    def test_порт_строкой_откатывается_к_умолчанию(self) -> None:
        self.записать_env("PORT=восемь\n")
        self.assertEqual(config.load().port, 8080)


class ТестыПроверкиНастроек(НастройкиНаВремя):
    def test_check_называет_чего_не_хватает(self) -> None:
        db_file, config.DB_FILE = config.DB_FILE, config.ROOT / "shop.db"
        try:
            with contextlib.redirect_stdout(io.StringIO()) as вывод:
                код = config._check()
        finally:
            config.DB_FILE = db_file
        self.assertEqual(код, 1)
        self.assertIn("чего не хватает", вывод.getvalue())
        self.assertIn("(ещё нет)", вывод.getvalue())  # базы в пустой папке нет

    def test_check_молчит_когда_всё_на_месте(self) -> None:
        self.записать_env(
            "TELEGRAM_BOT_TOKEN=123:ABC\n"
            "ADMIN_IDS=1\n"
            "WEBAPP_URL=https://example.org/app/\n"
        )
        db_file, config.DB_FILE = config.DB_FILE, config.ROOT / "shop.db"
        config.DB_FILE.write_bytes(b"")
        try:
            with contextlib.redirect_stdout(io.StringIO()) as вывод:
                код = config._check()
        finally:
            config.DB_FILE = db_file
        self.assertEqual(код, 0)
        self.assertIn("всё на месте", вывод.getvalue())
        self.assertIn("(есть)", вывод.getvalue())


class ТестыКоманднойСтрокиБазы(БазаНаВремя):
    def запустить(self, *argv: str) -> tuple[int, str]:
        with contextlib.redirect_stdout(io.StringIO()) as вывод:
            код = db.main(list(argv))
        return код, вывод.getvalue()

    def test_без_аргументов_показывает_подсказку(self) -> None:
        код, вывод = self.запустить()
        self.assertEqual(код, 0)
        self.assertIn("--init", вывод)

    def test_init_создаёт_базу(self) -> None:
        код, вывод = self.запустить("--init")
        self.assertEqual(код, 0)
        self.assertIn("база готова", вывод)

    def test_demo_наливает_витрину(self) -> None:
        код, вывод = self.запустить("--demo")
        self.assertEqual(код, 0)
        self.assertIn(f"добавлено демо-товаров: {len(db.DEMO)}", вывод)
        товары = db.catalog()["products"]
        self.assertEqual(len(товары), len(db.DEMO))
        # Вещи без размерной сетки (шапка, сумка) заводятся как «один размер».
        self.assertTrue(any(t["sizes"][0]["size"] == db.ONE_SIZE for t in товары))

    def test_check_считает_остатки_и_заявки(self) -> None:
        db.seed_demo()
        товар = db.catalog()["products"][0]
        заказ = db.create_order(
            user_id=1, username="", customer_name="Вася", phone="+79001234567",
            delivery="", address="", comment="",
            items=[{"variant_id": товар["sizes"][0]["variant_id"], "quantity": 1}],
        )
        db.set_order_status(заказ["id"], "confirmed")

        код, вывод = self.запустить("--check")
        self.assertEqual(код, 0)
        self.assertIn("товаров в витрине:", вывод)
        self.assertIn("подтверждённых заявок: 1", вывод)
        self.assertIn("в резерве: 0", вывод)

    def test_check_без_базы_отправляет_к_init(self) -> None:
        config.DB_FILE.unlink()
        код, вывод = self.запустить("--check")
        self.assertEqual(код, 1)
        self.assertIn("--init", вывод)


class ТестыМелочейБазы(БазаНаВремя):
    def test_категория_по_слагу(self) -> None:
        self.assertEqual(db.category_by_slug("jackets")["name"], "Куртки")
        self.assertIsNone(db.category_by_slug("нет-такой"))

    def test_пустое_обновление_товара_ничего_не_делает(self) -> None:
        product_id = db.add_product(name="Куртка", price=1000)
        было = db.get_product(product_id)
        db.update_product(product_id, неизвестное_поле="значение")
        self.assertEqual(db.get_product(product_id), было)

    def test_фотографии_добавляются_по_порядку(self) -> None:
        product_id = db.add_product(name="Куртка", price=1000, photos=["первое.jpg"])
        db.add_photo(product_id, "второе.jpg")
        self.assertEqual(db.get_product(product_id)["photos"], ["первое.jpg", "второе.jpg"])

    def test_фото_у_товара_без_фотографий(self) -> None:
        product_id = db.add_product(name="Куртка", price=1000)
        db.add_photo(product_id, "единственное.jpg")
        self.assertEqual(db.get_product(product_id)["photos"], ["единственное.jpg"])

    def test_неизвестный_статус_заявки_отвергается(self) -> None:
        with self.assertRaises(ValueError):
            db.set_order_status(1, "может быть")

    def test_заявки_фильтруются_по_статусу(self) -> None:
        product_id = db.add_product(name="Куртка", price=1000, sizes={"M": 2})
        variant_id = db.get_product(product_id)["sizes"][0]["variant_id"]
        for _ in range(2):
            db.create_order(
                user_id=1, username="", customer_name="Вася", phone="+79001234567",
                delivery="", address="", comment="",
                items=[{"variant_id": variant_id, "quantity": 1}],
            )
        первая = db.orders()[-1]["id"]
        db.set_order_status(первая, "rejected")
        self.assertEqual(len(db.orders()), 2)
        self.assertEqual([o["id"] for o in db.orders(status="rejected")], [первая])

    def test_список_для_админки_показывает_скрытое(self) -> None:
        db.add_product(name="Скрытая куртка", price=1000, status="hidden")
        self.assertEqual(db.catalog()["products"], [])
        self.assertEqual(len(db.products_for_admin()), 1)

    def test_пустой_список_для_админки(self) -> None:
        self.assertEqual(db.products_for_admin(), [])

    def test_несуществующий_товар_и_заявка(self) -> None:
        self.assertIsNone(db.get_product(999))
        self.assertIsNone(db.get_order(999))

    def test_ошибка_внутри_транзакции_откатывает_запись(self) -> None:
        product_id = db.add_product(name="Куртка", price=1000)
        with self.assertRaises(RuntimeError):
            with db.connect() as conn:
                conn.execute("UPDATE products SET name = 'Другая' WHERE id = ?", (product_id,))
                raise RuntimeError("что-то пошло не так")
        self.assertEqual(db.get_product(product_id)["name"], "Куртка")


class ФейкСессия:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class ФейкБотЗапуска:
    def __init__(self, token: str = "", default: object = None) -> None:
        self.token = token
        self.session = ФейкСессия()
        self.menu_button = None

    async def get_chat_menu_button(self, **kwargs: object) -> object:
        return self.menu_button

    async def set_chat_menu_button(self, menu_button: object = None, **kwargs: object) -> None:
        self.menu_button = menu_button

    async def set_my_commands(self, commands: object, scope: object = None, **kwargs: object) -> None:
        self.commands = commands

    async def get_me(self) -> SimpleNamespace:
        return SimpleNamespace(username="mco_shop_bot")

    async def send_message(self, chat_id: int, text: str, **kwargs: object) -> None:
        self.sent = (chat_id, text)


class ФейкДиспетчер:
    def __init__(self, storage: object = None) -> None:
        self.data: dict = {}
        self.routers: list = []
        self.polled = False

    def __setitem__(self, key: str, value: object) -> None:
        self.data[key] = value

    def include_router(self, router: object) -> None:
        self.routers.append(router)

    async def start_polling(self, bot: object) -> None:
        self.polled = True


class ФейкСобытие:
    """Ожидание Ctrl+C, которое в тесте заканчивается сразу."""

    async def wait(self) -> None:
        return None


class ТестыЗапуска(БазаНаВремя, unittest.IsolatedAsyncioTestCase):
    """`run()` поднимает витрину и бота. Сеть при этом не трогается."""

    def настройки_запуска(self) -> config.Settings:
        обычные = настройки()
        return config.Settings(
            **{**обычные.__dict__, "port": 0}  # порт 0 — свободный выберет система
        )

    @contextlib.contextmanager
    def перехват_приложения(self):
        поймано: dict = {}
        настоящий = server.create_app

        def подмена(settings, notify_order, allow_unsigned=False):
            поймано["notify"] = notify_order
            поймано["unsigned"] = allow_unsigned
            return настоящий(settings, notify_order, allow_unsigned)

        with mock.patch.object(server, "create_app", подмена):
            yield поймано

    async def test_магазин_поднимает_витрину_и_бота(self) -> None:
        with mock.patch("aiogram.Bot", ФейкБотЗапуска), \
             mock.patch("aiogram.Dispatcher", ФейкДиспетчер), \
             self.перехват_приложения() as поймано:
            await app.run(self.настройки_запуска())

        self.assertFalse(поймано["unsigned"])  # на сервере анонимных заявок нет

    async def test_заявка_из_витрины_доходит_до_владельца(self) -> None:
        товар = db.get_product(db.add_product(name="Куртка", price=1000))
        заказ = db.create_order(
            user_id=500, username="", customer_name="Вася", phone="+79001234567",
            delivery="", address="", comment="",
            items=[{"variant_id": товар["sizes"][0]["variant_id"], "quantity": 1}],
        )
        отправленное: list = []

        async def фейк_уведомления(bot, settings, order_id):
            отправленное.append(order_id)

        with mock.patch("aiogram.Bot", ФейкБотЗапуска), \
             mock.patch("aiogram.Dispatcher", ФейкДиспетчер), \
             mock.patch.object(app, "notify_admins", фейк_уведомления), \
             self.перехват_приложения() as поймано:
            await app.run(self.настройки_запуска())
            await поймано["notify"](заказ["id"])

        self.assertEqual(отправленное, [заказ["id"]])

    async def test_web_only_поднимает_витрину_без_токена(self) -> None:
        # В боевом режиме витрина без бота живёт до Ctrl+C. В тесте ожидание
        # заканчивается сразу, а поднятый сервер нужно закрыть руками —
        # выход по `return` мимо `finally` этого не делает.
        поднятые: list = []
        настоящий_runner = app.aiohttp_web.AppRunner

        def запомнить(application):
            runner = настоящий_runner(application)
            поднятые.append(runner)
            return runner

        пустые = config.Settings(
            bot_token="", admin_ids=(), webapp_url="", shop_name="Mco shop",
            port=0, delivery_options=(), currency="₽",
        )
        try:
            with mock.patch.object(app, "asyncio", SimpleNamespace(Event=ФейкСобытие)), \
                 mock.patch.object(app.aiohttp_web, "AppRunner", запомнить), \
                 self.перехват_приложения() as поймано:
                await app.run(пустые, web_only=True)
                with self.assertLogs("магазин", level="INFO") as логи:
                    await поймано["notify"](7)
        finally:
            for runner in поднятые:
                await runner.cleanup()

        self.assertTrue(поймано["unsigned"])  # отладка в браузере без подписи
        self.assertIn("бот выключен", логи.output[0])


class ТестыТочкиВхода(БазаНаВремя):
    def запустить(self, *argv: str) -> tuple[int, str]:
        # basicConfig настраивает корневой логгер на весь процесс: без заглушки
        # остальные тесты шли бы вперемешку с логом aiohttp.
        with contextlib.redirect_stdout(io.StringIO()) as вывод, \
             mock.patch.object(app.logging, "basicConfig"):
            код = app.main(list(argv))
        return код, вывод.getvalue()

    def test_check_отвечает_за_проверку_настроек(self) -> None:
        with mock.patch.object(config, "_check", return_value=0) as проверка:
            self.assertEqual(self.запустить("--check")[0], 0)
        проверка.assert_called_once()

    def test_без_настроек_магазин_не_стартует(self) -> None:
        пустые = config.Settings(
            bot_token="", admin_ids=(), webapp_url="", shop_name="Mco shop",
            port=8080, delivery_options=(), currency="₽",
            problems=("TELEGRAM_BOT_TOKEN пуст",),
        )
        with mock.patch.object(config, "load", return_value=пустые):
            код, вывод = self.запустить()
        self.assertEqual(код, 1)
        self.assertIn("TELEGRAM_BOT_TOKEN пуст", вывод)

    def test_запуск_с_настройками_доходит_до_цикла(self) -> None:
        запущено: list = []

        async def фейк_run(settings, web_only=False):
            запущено.append(web_only)

        with mock.patch.object(config, "load", return_value=настройки()), \
             mock.patch.object(app, "run", фейк_run):
            код, _ = self.запустить()
        self.assertEqual((код, запущено), (0, [False]))

    def test_web_only_не_требует_токена(self) -> None:
        запущено: list = []

        async def фейк_run(settings, web_only=False):
            запущено.append(web_only)

        пустые = config.Settings(
            bot_token="", admin_ids=(), webapp_url="", shop_name="Mco shop",
            port=8080, delivery_options=(), currency="₽", problems=("нет токена",),
        )
        with mock.patch.object(config, "load", return_value=пустые), \
             mock.patch.object(app, "run", фейк_run):
            код, _ = self.запустить("--web-only")
        self.assertEqual((код, запущено), (0, [True]))

    def test_ctrl_c_останавливает_магазин_молча(self) -> None:
        async def падает(settings, web_only=False):
            raise KeyboardInterrupt

        with mock.patch.object(config, "load", return_value=настройки()), \
             mock.patch.object(app, "run", падает):
            with self.assertLogs("магазин", level="INFO") as логи:
                код, _ = self.запустить()
        self.assertEqual(код, 0)
        self.assertIn("остановлено", логи.output[-1])


if __name__ == "__main__":
    unittest.main()
