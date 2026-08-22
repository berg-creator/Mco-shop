"""Сбор каталога: экспорт канала (`src/importer.py`) и веб-превью (`src/scrape.py`).

Оба разбора эвристические и оба заканчиваются черновиком, который человек
правит глазами. Проверяется, что машина не врёт молча: пост без цены не станет
товаром, «12к» — это двенадцать тысяч, повтор размера — вторая вещь, а сеть,
которая не ответила, останавливает сбор, а не портит каталог.

Сеть здесь не трогается: `urlopen` подменяется заглушкой.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from src import config, db, importer, scrape
from tests.test_shop import БазаНаВремя

СТРАНИЦА = """
<div class="tgme_widget_message js-widget_message" data-post="shop/10">
  <a class="tgme_widget_message_photo_wrap" style="background-image:url('https://cdn/10.jpg')"></a>
  <div class="tgme_widget_message_text js-message_text">STONE ISLAND HOODIE<br/>Размер: L<br/>Стоимость: 17.990₽</div>
  <time datetime="2026-01-05T12:00:00+00:00"></time>
</div>
<div class="tgme_widget_message js-widget_message" data-post="shop/11">
  <a class="tgme_widget_message_reply" href="https://t.me/shop/10"></a>
  <div class="tgme_widget_message_text js-message_text">❗️ПРОДАНО❗️</div>
</div>
<div class="tgme_widget_message js-widget_message">без номера поста</div>
"""


class ФейкОтвет:
    """Ответ urlopen: контекстный менеджер с `read()`."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "ФейкОтвет":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class ТестыРазбораЭкспорта(unittest.TestCase):
    def test_текст_приходит_строкой_списком_или_ничем(self) -> None:
        self.assertEqual(importer.plain_text("просто строка"), "просто строка")
        self.assertEqual(
            importer.plain_text(["кусок ", {"type": "bold", "text": "жирный"}, {"нет": "текста"}, 42]),
            "кусок жирный",
        )
        self.assertEqual(importer.plain_text(None), "")

    def test_маленькое_число_не_цена_а_доставка(self) -> None:
        # Первый шаблон найдёт «100 ₽», но одежды за сто рублей не бывает —
        # разбор идёт дальше и берёт настоящую цену из строки «цена».
        self.assertEqual(importer.find_price("доставка 100 ₽\nцена 8500"), 8500)

    def test_категория_угадывается_по_словам(self) -> None:
        self.assertEqual(importer.find_category("Тёплая куртка"), "jackets")
        self.assertEqual(importer.find_category("cargo pants"), "pants")
        self.assertEqual(importer.find_category("непонятная вещь"), "")

    def test_название_это_первая_содержательная_строка(self) -> None:
        текст = "•\n— Stone Island Куртка Ghost 42000 ₽\nЦена: 42000 ₽"
        self.assertEqual(importer.guess_name(текст, "Stone Island"), "Куртка Ghost")

    def test_строка_из_одного_бренда_пропускается(self) -> None:
        self.assertEqual(
            importer.guess_name("Stone Island\nКуртка Ghost", "Stone Island"), "Куртка Ghost"
        )

    def test_текст_без_названия_подписывается_честно(self) -> None:
        self.assertEqual(importer.guess_name("\n-\n", ""), "Без названия")


class ТестыФайлаЭкспорта(БазаНаВремя):
    def экспорт(self, messages: list | dict) -> Path:
        path = config.DATA / "result.json"
        path.write_text(json.dumps(messages, ensure_ascii=False), encoding="utf-8")
        return path

    def test_альбом_и_посты_без_цены(self) -> None:
        (config.DATA / "photos@1.jpg").write_bytes(b"\xff\xd8\xff")
        path = self.экспорт({
            "messages": [
                {"type": "service", "id": 1, "text": "канал создан"},
                {"type": "message", "id": 2, "text": "просто мысли вслух"},
                {"type": "message", "id": 3, "text": "", "photo": "photos@1.jpg"},
                {"type": "message", "id": 4,
                 "text": ["Куртка ", {"text": "Stone Island"}, "\nразмер M\nцена 42 000 ₽"],
                 "photo": "photos@1.jpg", "date": "2026-01-05T12:00:00"},
                {"type": "message", "id": 5, "text": "", "photo": "photos@1.jpg"},
                {"type": "message", "id": 6, "text": "ПРОДАНО, забрали\nразмер L\n9000 ₽"},
            ]
        })
        drafts = importer.parse_export(path)

        self.assertEqual([d["post_id"] for d in drafts], [4, 6])
        куртка = drafts[0]
        self.assertEqual(куртка["brand"], "Stone Island")
        self.assertEqual(куртка["category"], "jackets")
        self.assertEqual(куртка["price"], 42000)
        self.assertEqual(куртка["sizes"], {"M": 1})
        self.assertEqual(len(куртка["photos"]), 2)  # своё фото и фото из альбома
        self.assertFalse(куртка["sold"])
        self.assertTrue(drafts[1]["sold"])

    def test_экспорт_голым_списком_тоже_читается(self) -> None:
        path = self.экспорт([{"type": "message", "id": 1, "text": "Худи\nразмер L\n9000 ₽"}])
        self.assertEqual(len(importer.parse_export(path)), 1)

    def test_вещь_без_размера_получает_один_размер(self) -> None:
        path = self.экспорт([{"type": "message", "id": 1, "text": "Шапка\n7000 ₽"}])
        self.assertEqual(importer.parse_export(path)[0]["sizes"], {db.ONE_SIZE: 1})


class ТестыФотографий(БазаНаВремя):
    def test_фото_по_ссылке_скачивается(self) -> None:
        with mock.patch("urllib.request.urlopen", return_value=ФейкОтвет(b"\xff\xd8\xff")):
            имя = importer.save_photo("https://cdn/1.jpg")
        self.assertTrue((config.PHOTOS / имя).exists())

    def test_недоступная_ссылка_не_роняет_импорт(self) -> None:
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("нет сети")), \
             contextlib.redirect_stdout(io.StringIO()) as вывод:
            имя = importer.save_photo("https://cdn/1.jpg")
        self.assertIsNone(имя)
        self.assertIn("не скачалось", вывод.getvalue())

    def test_файл_с_диска_копируется_к_себе(self) -> None:
        источник = config.DATA / "снимок.jpg"
        источник.write_bytes(b"\xff\xd8\xff")
        имя = importer.save_photo(str(источник))
        self.assertEqual((config.PHOTOS / имя).read_bytes(), b"\xff\xd8\xff")

    def test_пропавшего_файла_достаточно_чтобы_пропустить_фото(self) -> None:
        self.assertIsNone(importer.save_photo(str(config.DATA / "нет.jpg")))


class ТестыЗаливки(БазаНаВремя):
    def test_проданное_не_заливается_а_с_ключом_заливается(self) -> None:
        drafts = [{"post_id": 1, "name": "Куртка", "price": 9000, "sizes": {"M": 1}, "sold": True}]
        self.assertEqual(importer.apply_drafts(drafts), 0)
        self.assertEqual(importer.apply_drafts(drafts, skip_sold=False), 1)

    def test_черновик_заливается_со_скидкой_фото_и_категорией(self) -> None:
        источник = config.DATA / "снимок.jpg"
        источник.write_bytes(b"\xff\xd8\xff")
        added = importer.apply_drafts([{
            "post_id": 7, "name": "Куртка", "brand": "Stone Island", "category": "jackets",
            "price": 24000, "old_price": 30000, "sizes": {"M": 1},
            "description": "Носили сезон, б/у", "photos": [str(источник)],
        }])
        self.assertEqual(added, 1)
        товар = db.catalog()["products"][0]
        self.assertEqual(товар["old_price"], 30000)
        self.assertEqual(товар["condition"], "used")
        self.assertEqual(товар["category"], "jackets")
        self.assertEqual(len(товар["photos"]), 1)


class ТестыКоманднойСтрокиИмпорта(БазаНаВремя):
    def запустить(self, *argv: str) -> tuple[int, str]:
        with contextlib.redirect_stdout(io.StringIO()) as вывод:
            код = importer.main(list(argv))
        return код, вывод.getvalue()

    def подготовить_экспорт(self) -> Path:
        path = config.DATA / "result.json"
        path.write_text(json.dumps({"messages": [
            {"type": "message", "id": 1, "text": "Stone Island Куртка\nразмер M\n42 000 ₽"},
            {"type": "message", "id": 2, "text": "Худи ПРОДАНО\n9000 ₽"},
        ]}, ensure_ascii=False), encoding="utf-8")
        return path

    def test_без_аргументов_показывает_подсказку(self) -> None:
        код, вывод = self.запустить()
        self.assertEqual(код, 1)
        self.assertIn("--apply", вывод)

    def test_несуществующий_файл_называется(self) -> None:
        код, вывод = self.запустить(str(config.DATA / "нет.json"))
        self.assertEqual(код, 1)
        self.assertIn("файла нет", вывод)

    def test_разбор_печатает_отчёт_и_зовёт_дальше(self) -> None:
        код, вывод = self.запустить(str(self.подготовить_экспорт()))
        self.assertEqual(код, 0)
        self.assertIn("разобрано постов с ценой: 2", вывод)
        self.assertIn("помечены проданными:    1", вывод)
        self.assertIn("Что дальше", вывод)
        self.assertEqual(db.catalog()["products"], [])  # разбор ничего не пишет в базу

    def test_dry_run_ничего_не_предлагает(self) -> None:
        код, вывод = self.запустить(str(self.подготовить_экспорт()), "--dry-run")
        self.assertEqual(код, 0)
        self.assertNotIn("Что дальше", вывод)

    def test_черновик_сохраняется_и_заливается(self) -> None:
        черновик = config.DATA / "draft.json"
        код, вывод = self.запустить(str(self.подготовить_экспорт()), "--out", str(черновик))
        self.assertEqual(код, 0)
        self.assertIn("черновик записан", вывод)

        код, вывод = self.запустить("--apply", str(черновик))
        self.assertEqual(код, 0)
        self.assertIn("добавлено товаров: 1", вывод)  # проданное осталось за бортом

        код, _ = self.запустить("--apply", str(черновик), "--with-sold")
        self.assertEqual(len(db.products_for_admin()), 3)


class ТестыРазбораСтраницы(unittest.TestCase):
    def test_страница_разбирается_на_посты_и_ответы(self) -> None:
        посты = scrape.parse_page(СТРАНИЦА)
        self.assertEqual([p["id"] for p in посты], [10, 11])  # блок без номера пропущен
        карточка, ответ = посты
        self.assertEqual(карточка["photos"], ["https://cdn/10.jpg"])
        self.assertEqual(карточка["date"], "2026-01-05T12:00:00+00:00")
        self.assertIn("Размер: L", карточка["text"])
        self.assertEqual(ответ["reply_to"], 10)

    def test_разметка_превращается_в_текст(self) -> None:
        self.assertEqual(scrape.clean_text("<b>Куртка</b><br/>M &amp; L"), "Куртка\nM & L")

    def test_страница_забирается_из_сети_как_браузером(self) -> None:
        with mock.patch("urllib.request.urlopen", return_value=ФейкОтвет("<html>привет".encode())):
            self.assertEqual(scrape.fetch("https://t.me/s/shop"), "<html>привет")

    def test_мелочи_разбора(self) -> None:
        self.assertIsNone(scrape.parse_money("цена в личку"))
        self.assertEqual(scrape.find_brand("непонятная вещь"), "")
        self.assertEqual(scrape.strip_brand("NOGLETCHER", ""), "NOGLETCHER")
        self.assertEqual(scrape.find_category("непонятная вещь"), "")
        # Слишком длинный кусок и не похожий на размер отбрасываются.
        self.assertEqual(scrape.parse_sizes("M, оченьдлинный, ??? , "), {"M": 1})
        # Единица измерения рядом с числом — это не часть размера.
        self.assertEqual(scrape.parse_sizes("41, 41, 42 EUR"), {"41": 2, "42": 1})
        self.assertEqual(scrape.parse_sizes("EU 41 / US 8"), {"41": 1, "8": 1})
        self.assertIsNone(scrape.parse_product({"id": 1, "text": "  \n ", "photos": [], "date": ""}))


class ТестыОбходаКанала(unittest.TestCase):
    def страницы(self, *тела: str):
        """urlopen, отдающий заготовленные страницы по очереди."""
        ответы = [ФейкОтвет(тело.encode()) for тело in тела]
        return mock.patch("urllib.request.urlopen", side_effect=ответы)

    def test_обход_идёт_до_конца_архива(self) -> None:
        первая = СТРАНИЦА + '<a class="tme_messages_more" data-before="10"></a>'
        with self.страницы(первая, СТРАНИЦА), \
             mock.patch("time.sleep") as пауза, \
             contextlib.redirect_stdout(io.StringIO()):
            посты = scrape.collect_messages("shop", max_pages=5, pause=0.1)
        self.assertEqual([p["id"] for p in посты], [10, 11])  # повторы схлопнулись
        пауза.assert_called_once_with(0.1)

    def test_лимит_страниц_останавливает_обход(self) -> None:
        первая = СТРАНИЦА + '<a class="tme_messages_more" data-before="10"></a>'
        with self.страницы(первая), mock.patch("time.sleep"), \
             contextlib.redirect_stdout(io.StringIO()):
            посты = scrape.collect_messages("shop", max_pages=1)
        self.assertEqual([p["id"] for p in посты], [10, 11])

    def test_пустая_страница_останавливает_обход(self) -> None:
        with self.страницы("<html>ничего</html>"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(scrape.collect_messages("shop"), [])

    def test_ошибка_сервера_останавливает_сбор(self) -> None:
        ошибка = urllib.error.HTTPError(
            "https://t.me/s/shop", 429, "Too Many", None, io.BytesIO(b"")
        )
        self.addCleanup(ошибка.close)
        with mock.patch("urllib.request.urlopen", side_effect=ошибка), \
             contextlib.redirect_stdout(io.StringIO()) as вывод:
            self.assertEqual(scrape.collect_messages("shop"), [])
        self.assertIn("429", вывод.getvalue())

    def test_нет_связи_останавливает_сбор(self) -> None:
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("нет сети")), \
             contextlib.redirect_stdout(io.StringIO()) as вывод:
            self.assertEqual(scrape.collect_messages("shop"), [])
        self.assertIn("нет связи", вывод.getvalue())


class ТестыОбновленийИзКанала(unittest.TestCase):
    def сообщение(self, post_id: int, text: str, reply_to: int | None = None) -> dict:
        return {"id": post_id, "text": text, "reply_to": reply_to, "photos": [], "date": ""}

    def test_черновики_собираются_вместе_с_ответами(self) -> None:
        сообщения = [
            self.сообщение(1, "STONE ISLAND HOODIE\nРазмер: L, XL\nСтоимость: 17.990₽"),
            self.сообщение(2, "❗️L ПРОДАНО❗️", reply_to=1),
            self.сообщение(3, "просто анонс без цены"),
            self.сообщение(4, "ответ на чужой пост", reply_to=999),
        ]
        drafts, skipped = scrape.build_drafts(сообщения)
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0]["sizes"], {"XL": 1})  # проданный размер ушёл
        self.assertEqual([m["id"] for m in skipped], [3])

    def test_последний_размер_закрывает_вещь(self) -> None:
        сообщения = [
            self.сообщение(1, "HOODIE\nРазмер: L\nСтоимость: 17.990₽"),
            self.сообщение(2, "❗️L ПРОДАНО❗️", reply_to=1),
        ]
        drafts, _ = scrape.build_drafts(сообщения)
        self.assertTrue(drafts[0]["sold"])

    def test_продажа_размера_которого_нет_остаётся_человеку(self) -> None:
        сообщения = [
            self.сообщение(1, "HOODIE\nРазмер: L\nСтоимость: 17.990₽"),
            self.сообщение(2, "❗️XS ПРОДАНО❗️", reply_to=1),
        ]
        drafts, _ = scrape.build_drafts(сообщения)
        self.assertIn("продан размер XS", drafts[0]["notes"][0])

    def test_обещание_новой_цены_без_чисел_ничего_не_меняет(self) -> None:
        сообщения = [
            self.сообщение(1, "HOODIE\nРазмер: L\nСтоимость: 17.990₽"),
            self.сообщение(2, "завтра будет новая цена", reply_to=1),
        ]
        drafts, _ = scrape.build_drafts(сообщения)
        self.assertEqual(drafts[0]["price"], 17990)

    def test_пост_без_текста_не_попадает_даже_в_пропущенные(self) -> None:
        сообщения = [
            self.сообщение(1, "HOODIE\nРазмер: L\nСтоимость: 17.990₽"),
            self.сообщение(2, ""),
        ]
        drafts, skipped = scrape.build_drafts(сообщения)
        self.assertEqual((len(drafts), skipped), (1, []))

    def test_одна_цена_в_объявлении_просто_меняет_цену(self) -> None:
        сообщения = [
            self.сообщение(1, "HOODIE\nРазмер: L\nСтоимость: 17.990₽"),
            self.сообщение(2, "8.000₽ — новая цена", reply_to=1),
        ]
        drafts, _ = scrape.build_drafts(сообщения)
        self.assertEqual((drafts[0]["price"], drafts[0]["old_price"]), (8000, None))


class ТестыКоманднойСтрокиСбора(БазаНаВремя):
    def запустить(self, *argv: str) -> tuple[int, str]:
        with contextlib.redirect_stdout(io.StringIO()) as вывод:
            код = scrape.main(list(argv))
        return код, вывод.getvalue()

    def сообщения(self) -> list[dict]:
        return [
            {"id": 1, "reply_to": None, "photos": ["https://cdn/1.jpg"], "date": "",
             "text": "RICK OWENS DRKSHDW BOOTS\nРазмер: 41, 41, 42 EUR\n"
                     "Состояние: б/у, носили\nСтоимость: 30.000₽\nКупить: @shop\n#мужское"},
            {"id": 2, "reply_to": 1, "photos": [], "date": "",
             "text": "привезли ещё пару размеров"},
            {"id": 3, "reply_to": None, "photos": [], "date": "",
             "text": "STONE ISLAND HOODIE\nРазмер: L\nСтоимость: 17.990₽"},
            {"id": 4, "reply_to": 3, "photos": [], "date": "", "text": "❗️ПРОДАНО❗️"},
            {"id": 5, "reply_to": None, "photos": [], "date": "",
             "text": "можем привезти сумки под заказ, стоимость 12.000₽"},
        ]

    def test_отчёт_показывает_наличие_примечания_и_пропуски(self) -> None:
        with mock.patch.object(scrape, "collect_messages", return_value=self.сообщения()):
            код, вывод = self.запустить("shop", "--dry-run")
        self.assertEqual(код, 0)
        self.assertIn("всего карточек:        2", вывод)
        self.assertIn("в наличии:           1", вывод)
        self.assertIn("продано:             1", вывод)
        self.assertIn("41×2, 42×1", вывод)  # повтор размера — это две пары
        self.assertIn("примечание из канала: привезли ещё пару размеров", вывод)
        self.assertIn("пропущенные посты", вывод)

    def test_черновик_сохраняется_на_правку(self) -> None:
        черновик = config.DATA / "draft.json"
        with mock.patch.object(scrape, "collect_messages", return_value=self.сообщения()):
            код, вывод = self.запустить("shop", "--out", str(черновик))
        self.assertEqual(код, 0)
        self.assertIn("черновик записан", вывод)
        карточки = json.loads(черновик.read_text(encoding="utf-8"))
        сапоги = карточки[0]
        self.assertEqual(сапоги["brand"], "Rick Owens DRKSHDW")
        self.assertEqual(сапоги["category"], "shoes")
        self.assertEqual(сапоги["condition"], "used")
        self.assertEqual(сапоги["price"], 30000)
        self.assertNotIn("@shop", сапоги["description"])  # покупателя не уводят из бота
        self.assertNotIn("#", сапоги["description"])

    def test_отчёт_без_пропусков_не_печатает_лишнего(self) -> None:
        карточка = [m for m in self.сообщения() if m["id"] == 3]
        with mock.patch.object(scrape, "collect_messages", return_value=карточка):
            код, вывод = self.запустить("shop", "--dry-run")
        self.assertEqual(код, 0)
        self.assertNotIn("пропущенные посты", вывод)

    def test_без_ключей_подсказывает_следующий_шаг(self) -> None:
        with mock.patch.object(scrape, "collect_messages", return_value=self.сообщения()):
            код, вывод = self.запустить("shop")
        self.assertEqual(код, 0)
        self.assertIn("Что дальше", вывод)

    def test_молчащий_канал_это_ошибка_запуска(self) -> None:
        with mock.patch.object(scrape, "collect_messages", return_value=[]):
            код, вывод = self.запустить("shop")
        self.assertEqual(код, 1)
        self.assertIn("канал ничего не отдал", вывод)


if __name__ == "__main__":
    unittest.main()
