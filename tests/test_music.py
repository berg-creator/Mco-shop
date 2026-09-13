"""Темп присланных песен: измерение, плейлист и жизнь без ffmpeg.

Проверяется главное — что детектор действительно слышит долю: ему даётся
щёлкающая дорожка с известным темпом и известной фазой, и он обязан назвать
их обратно. Витрину на глаз тут не проверишь: расхождение в полдоли видно
только через минуту прослушивания, когда снимок начинает опаздывать.

Остальное — про то, что ffmpeg на сервере может отсутствовать, вернуть
мусор или упасть: песня в этом случае играет, а листание идёт по умолчанию.
"""

from __future__ import annotations

import array
import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import config, music

BPM = 96.0
OFFSET = 0.25  # секунда первой доли
SECONDS = 40


def щелчки(bpm: float = BPM, offset: float = OFFSET, seconds: int = SECONDS) -> array.array:
    """Дорожка из щелчков: тишина, а на каждой доле — короткий громкий всплеск.

    Каждый четвёртый щелчок громче: это сильная доля, по ней детектор
    и должен ставить фазу листания.
    """
    samples = array.array("h", bytes(2 * music.RATE * seconds))
    beat = 60.0 / bpm
    index = 0
    moment = offset
    while moment < seconds - beat:
        start = int(moment * music.RATE)
        loud = 12000 if index % 4 else 30000
        for shift in range(int(0.02 * music.RATE)):
            # Знак через шаг: щелчок должен быть слышен как звук, а не как
            # постоянное смещение, которое любой фильтр выкинет.
            samples[start + shift] = loud if shift % 2 else -loud
        index += 1
        moment += beat
    return samples


class ТестыИзмерения(unittest.TestCase):
    def test_детектор_слышит_долю_и_фазу(self) -> None:
        info = music.detect(music.envelope(щелчки()))
        self.assertAlmostEqual(info["bpm"], BPM, delta=0.5)
        # 96 BPM — доля 625 мс, четыре доли ближе всего к трём секундам.
        self.assertAlmostEqual(info["flip_ms"], 2500, delta=20)
        # Фаза — на сильной доле, с точностью до кадра огибающей.
        self.assertAlmostEqual(info["offset_ms"], OFFSET * 1000, delta=40)

    def test_ошибка_темпа_меньше_доли_за_трек(self) -> None:
        """Точность важнее красоты числа: 0,5 BPM за три минуты — это полдоли."""
        info = music.detect(music.envelope(щелчки()))
        уход = abs(info["bpm"] - BPM) / BPM * 180  # секунд за трёхминутный трек
        self.assertLess(уход, 60.0 / BPM / 2)

    def test_короткому_куску_темпа_нет(self) -> None:
        self.assertIsNone(music.detect(music.envelope(щелчки(seconds=3))))

    def test_короткая_петля_растягивается_до_трёх_секунд(self) -> None:
        """На шести секундах дальше двух период искать негде — шаг удваивается.

        Удвоение периода остаётся на сетке музыки, поэтому проверяем не
        секунды, а доли: снимок обязан меняться на ударе.
        """
        info = music.detect(music.envelope(щелчки(seconds=6)))
        ударов = info["flip_ms"] / 1000 / (60.0 / BPM)
        self.assertAlmostEqual(ударов, round(ударов), delta=0.02)
        self.assertGreater(info["flip_ms"], 2000)

    def test_длинная_петля_делится_пополам(self) -> None:
        """У медленной музыки петля длиннее четырёх секунд: столько на снимке
        не держат, и шаг делится пополам — оставаясь целым числом ударов."""
        медленно = 48.0  # петля в четыре удара — это пять секунд
        info = music.detect(music.envelope(щелчки(bpm=медленно)))
        ударов = info["flip_ms"] / 1000 / (60.0 / медленно)
        self.assertAlmostEqual(ударов, round(ударов), delta=0.02)
        self.assertLess(info["flip_ms"], music.TARGET_FLIP * 1500)

    def test_шаг_всегда_попадает_по_ударам(self) -> None:
        """Октаву детектор может выбрать любую — на витрине это не видно.

        Вдвое частые щелчки он вправе назвать и вдвое медленнее: у складки
        это одно и то же, а половинный уровень она и предпочитает. Важно
        другое — шаг листания остаётся целым числом ударов и держится около
        трёх секунд, то есть снимок меняется в такт.
        """
        удар = 60.0 / (BPM * 2)
        info = music.detect(music.envelope(щелчки(bpm=BPM * 2)))
        ударов = info["flip_ms"] / 1000 / удар
        self.assertAlmostEqual(ударов, round(ударов), delta=0.02)
        self.assertTrue(1.5 <= info["flip_ms"] / 1000 <= 5.0, info["flip_ms"])


class ТестыДекодирования(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.file = Path(self._temp.name) / "track.mp3"
        self.file.write_bytes(b"\xff\xfb\x90")

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_без_ffmpeg_темпа_нет(self) -> None:
        with mock.patch("shutil.which", return_value=None):
            self.assertIsNone(music.analyze(self.file))

    def test_ffmpeg_упал_на_чужом_файле(self) -> None:
        with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             mock.patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "ffmpeg")):
            self.assertIsNone(music.analyze(self.file))

    def test_пустой_звук_это_не_музыка(self) -> None:
        with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             mock.patch("subprocess.run", return_value=mock.Mock(stdout=b"")):
            self.assertIsNone(music.analyze(self.file))

    def test_темп_считается_по_отсчётам_из_ffmpeg(self) -> None:
        # Нечётный хвост в конце: оборванный отсчёт не должен ронять разбор.
        pcm = щелчки().tobytes() + b"\x00"
        with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             mock.patch("subprocess.run", return_value=mock.Mock(stdout=pcm)):
            info = music.analyze(self.file)
        self.assertAlmostEqual(info["bpm"], BPM, delta=0.5)


class ТестыПлейлиста(unittest.TestCase):
    """Плейлист магазина: песни складываются в список, у каждой свой темп."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self._music = config.MUSIC
        config.MUSIC = Path(self._temp.name)

    def tearDown(self) -> None:
        config.MUSIC = self._music
        self._temp.cleanup()

    def положить(self, name: str, **info: float) -> None:
        (config.MUSIC / name).write_bytes(b"song")
        music.add(name, name, {**music.DEFAULT_FLIP, **info})

    def test_расширение_из_типа_файла(self) -> None:
        self.assertEqual(music.suffix_for("audio/mp4"), ".m4a")
        self.assertEqual(music.suffix_for("AUDIO/MPEG"), ".mp3")
        # Незнакомый тип — mp3: так Telegram присылает почти всё.
        self.assertEqual(music.suffix_for(""), ".mp3")

    def test_имя_не_занимает_чужое(self) -> None:
        """Номер ищется по занятым именам, а не по длине плейлиста: пустой
        файл, которым обработчик занял имя, тоже считается занятым."""
        self.assertEqual(music.free_name(".mp3"), "track-1.mp3")
        (config.MUSIC / "track-1.mp3").write_bytes(b"song")
        self.assertEqual(music.free_name(".m4a"), "track-2.m4a")

    def test_у_каждой_песни_свой_темп(self) -> None:
        self.положить("track-1.mp3", bpm=90.0, flip_ms=2666)
        self.положить("track-2.m4a", bpm=174.0, flip_ms=2758)

        плейлист = music.playlist()
        self.assertEqual([трек["bpm"] for трек in плейлист], [90.0, 174.0])
        self.assertEqual(плейлист[1]["flip_ms"], 2758)
        self.assertTrue(плейлист[1]["url"].startswith("/music/track-2.m4a?v="))

    def test_повторное_имя_не_двоит_песню(self) -> None:
        self.положить("track-1.mp3", bpm=90.0)
        self.положить("track-1.mp3", bpm=120.0)
        плейлист = music.playlist()
        self.assertEqual(len(плейлист), 1)
        self.assertEqual(плейлист[0]["bpm"], 120.0)

    def test_одиночный_трек_прежних_версий_уносится(self) -> None:
        """До плейлиста в магазине играла одна песня — она больше не нужна."""
        (config.MUSIC / "track.mp3").write_bytes(b"old")
        (config.MUSIC / "track.json").write_text("{}", encoding="utf-8")
        self.положить("track-1.mp3", bpm=90.0)

        self.assertFalse((config.MUSIC / "track.mp3").exists())
        self.assertFalse((config.MUSIC / "track.json").exists())
        self.assertEqual(len(music.playlist()), 1)

    def test_без_плейлиста_в_витрине_тихо(self) -> None:
        self.assertEqual(music.playlist(), [])

    def test_битая_запись_не_роняет_каталог(self) -> None:
        (config.MUSIC / music.LIST_FILE).write_text("{это не json", encoding="utf-8")
        self.assertEqual(music.playlist(), [])
        # И запись не тем типом — тоже не повод отдать каталог с ошибкой.
        (config.MUSIC / music.LIST_FILE).write_text("42", encoding="utf-8")
        self.assertEqual(music.playlist(), [])

    def test_запись_без_файла_не_считается(self) -> None:
        """Песню унесли руками — витрина играет остальные, а не 404."""
        self.положить("track-1.mp3", bpm=90.0)
        self.положить("track-2.mp3", bpm=120.0)
        (config.MUSIC / "track-1.mp3").unlink()

        плейлист = music.playlist()
        self.assertEqual([трек["bpm"] for трек in плейлист], [120.0])

    def test_музыку_можно_убрать_целиком(self) -> None:
        self.положить("track-1.mp3", bpm=90.0)
        music.forget()
        self.assertEqual(music.playlist(), [])
        self.assertEqual(list(config.MUSIC.glob("track*")), [])


class ТестыКоманднойСтроки(unittest.TestCase):
    """`python -m src.music файл` — посмотреть темп, `--add` — положить в плейлист."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self._music = config.MUSIC
        config.MUSIC = Path(self._temp.name) / "music"

    def tearDown(self) -> None:
        config.MUSIC = self._music
        self._temp.cleanup()

    def песня(self, name: str) -> Path:
        path = Path(self._temp.name) / name
        path.write_bytes(b"\xff\xfb\x90")
        return path

    def прогон(self, argv: list[str], **patch) -> tuple[int, str]:
        with contextlib.redirect_stdout(io.StringIO()) as вывод:
            if patch:
                with mock.patch.object(music, "analyze", **patch):
                    code = music.main(argv)
            else:
                code = music.main(argv)
        return code, вывод.getvalue()

    def test_без_файла_подсказка(self) -> None:
        code, вывод = self.прогон([])
        self.assertEqual(code, 2)
        self.assertIn("нужен файл", вывод)

    def test_не_музыка_это_не_успех(self) -> None:
        code, вывод = self.прогон(["track.mp3"], return_value=None)
        self.assertEqual(code, 1)
        self.assertIn("не определился", вывод)

    def test_темп_печатается(self) -> None:
        code, вывод = self.прогон(["track.mp3"], return_value={"bpm": 73.5, "flip_ms": 3265, "offset_ms": 115})
        self.assertEqual(code, 0)
        self.assertIn("73.5 BPM", вывод)

    def test_альбом_кладётся_с_диска(self) -> None:
        """`--add`: альбом в хорошем качестве в бота не влезает, а сюда — да."""
        песни = [self.песня("02 Renegade Snares.flac"), self.песня("03 Nu Birth of Cool.aiff")]
        code, вывод = self.прогон(
            ["--add", *map(str, песни)],
            return_value={"bpm": 174.0, "flip_ms": 2758, "offset_ms": 40},
        )

        self.assertEqual(code, 0)
        self.assertIn("174.0 BPM", вывод)
        плейлист = music.playlist()
        self.assertEqual([трек["title"] for трек in плейлист], [путь.stem for путь in песни])
        # Расширение сохраняется, если браузер его знает; чужое — станет mp3.
        self.assertTrue(плейлист[0]["url"].startswith("/music/track-1.flac?v="))
        self.assertTrue(плейлист[1]["url"].startswith("/music/track-2.mp3?v="))

    def test_неизмеренный_темп_не_повод_отказать(self) -> None:
        code, вывод = self.прогон(["--add", str(self.песня("тишина.mp3"))], return_value=None)
        self.assertEqual(code, 0)
        self.assertIn("не определился", вывод)
        self.assertEqual(music.playlist()[0]["flip_ms"], music.DEFAULT_FLIP["flip_ms"])

    def test_без_файлов_подсказка(self) -> None:
        code, вывод = self.прогон(["--add"])
        self.assertEqual(code, 2)
        self.assertIn("нужны файлы", вывод)

    def test_несуществующий_файл_виден_сразу(self) -> None:
        code, вывод = self.прогон(["--add", "альбом/нет-такого.mp3"])
        self.assertEqual(code, 1)
        self.assertIn("нет такого файла", вывод)
        self.assertEqual(music.playlist(), [])


if __name__ == "__main__":
    unittest.main()
