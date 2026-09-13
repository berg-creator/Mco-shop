"""Плейлист витрины и ритм листания фотографий под то, что играет сейчас.

Владелец присылает боту песни — витрина играет их подряд, тасуя порядок
на каждом открытии, а снимки в карточке товара листаются в темпе той песни,
которая идёт в эту минуту. Темп меряется по самому файлу, а не спрашивается:
человек, который знает BPM своей песни, — редкость, а ошибка в пару процентов
за три минуты уводит листание мимо музыки.

Темп у каждой песни свой и хранится рядом с ней: в альбоме соседние вещи
расходятся на десятки ударов, и один темп на весь плейлист означал бы, что
после первой же смены трека витрина качается мимо музыки.

Как меряется. Звук сводится в моно на 8 кГц (доли слышно и на ней),
из него берётся огибающая нарастания громкости — где стало громче, там
удар.

Дальше ищется не темп, а период: через сколько секунд огибающая повторяет
сама себя (автокорреляция на промежутке от полусекунды до восьми). Петля,
такт, фраза — что найдётся, то и годится, потому что шаг листания получается
делением этого периода пополам, а деление настоящего периода остаётся
на сетке музыки при любом ответе.

Сразу темпом это не считается намеренно. Складка огибающей «по кругу»,
которой темп искали раньше, на брейкбите выбирает долю мимо музыки: у джангла
на 155 ударах она уверенно называет 116 — три четверти доли, — и снимок
после каждого такта уезжает всё дальше. Поэтому доля ищется только среди
целых делителей найденного периода: те, что его не делят, лежат мимо музыки
по построению. Складка выбирает из них уровень и даёт фазу, а точное
значение доводится перебором вокруг него — ошибка в 0,05 BPM за шесть минут
трека набегает в сотню миллисекунд, то есть в четверть доли.

Метрический уровень при этом может оказаться половинным: у джангла на 155
складка часто предпочитает 77,5 — это та же музыка, посчитанная через раз.
На витрине разницы нет (шаг листания в секундах тот же), а в числе — есть,
и бот покажет то, что померил.

Декодирует ffmpeg. Разбор mp3 на Python ради одной команды — это тысячи
строк и чужой формат в проекте про одежду; ffmpeg ставится одной строкой
в `scripts/deploy.sh`. Если его нет, песня всё равно играет — фото просто
листаются раз в три секунды, о чём бот честно пишет.

Файлы лежат в `data/music/` — рядом с базой и фотографиями, а не в `webapp/`:
выкладка новой версии затирает `webapp/` целиком, и присланные песни уезжали бы
при каждом `deploy.sh`.

    python -m src.music <файл>          # что за темп в файле, ничего не сохраняя
    python -m src.music --add <файлы>   # положить песни в плейлист витрины
"""

from __future__ import annotations

import array
import json
import math
import shutil
import subprocess
import sys
from operator import mul
from pathlib import Path

from . import config

# Частота, до которой сводится звук. Выше незачем: бочка и снейр живут
# в нижней половине спектра, а каждый лишний килогерц — лишняя секунда счёта.
RATE = 8000
# Кадр огибающей — 20 мс. Это же и точность фазы: на глаз сдвиг в 20 мс
# в листании фотографий не читается.
HOP = 160
# Границы поиска. Ниже 60 и выше 180 живёт разве что дабстеп и вальс,
# а половинный и двойной темп ловится всё равно — см. FLIP_BEATS.
BPM_LOW, BPM_HIGH = 60.0, 180.0
# Сколько секунд должен длиться показ одного снимка. Три — это «успел
# рассмотреть, но не заскучал»; точное значение подгоняется к долям.
TARGET_FLIP = 3.0
# Где искать период: короче полусекунды — это доля или её половина, длиннее
# восьми — уже фраза, и делить её пополам приходится слишком много раз.
PERIOD_LOW, PERIOD_HIGH = 0.5, 8.0
# Короче четырёх секунд мерить нечего: это меньше пяти долей.
MIN_FRAMES = 200

LIST_FILE = "tracks.json"
# Расширение выбирается по типу, который прислал Telegram: браузер ищет
# проигрыватель по нему, и m4a под именем .mp3 на iPhone просто молчит.
SUFFIX_BY_MIME = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".m4a",
    "audio/ogg": ".ogg",
    "audio/opus": ".ogg",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
}
# Чем листать, когда темп не определился.
DEFAULT_FLIP = {"bpm": 0.0, "flip_ms": 3000, "offset_ms": 0}


def suffix_for(mime: str) -> str:
    """Расширение файла по типу, который прислал Telegram."""
    return SUFFIX_BY_MIME.get((mime or "").lower(), ".mp3")


def free_name(suffix: str) -> str:
    """Свободное имя для новой песни: номер по порядку и это расширение.

    Имя не от названия трека: в названиях кириллица, скобки и косые черты,
    а это имя уезжает в адрес витрины. Как песня называется, помнит плейлист.
    """
    number = 1
    while any(config.MUSIC.glob(f"track-{number}.*")):
        number += 1
    return f"track-{number}{suffix}"


def _pcm(path: Path) -> array.array | None:
    """Отдаёт звук файла как моно-отсчёты. None — ffmpeg нет или файл не звук."""
    if not shutil.which("ffmpeg"):
        return None
    try:
        raw = subprocess.run(
            ["ffmpeg", "-v", "quiet", "-i", str(path), "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"],
            capture_output=True,
            timeout=120,
            check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return None
    samples = array.array("h")
    # Нечётный хвост — оборванный последний отсчёт: frombytes на нём падает.
    samples.frombytes(raw[: len(raw) // 2 * 2])
    return samples or None


def envelope(samples: array.array) -> list[float]:
    """Огибающая нарастания громкости: удар — это когда стало громче.

    Громкость берётся логарифмом: в тихом куплете и громком припеве удары
    должны весить одинаково, иначе весь темп посчитается по припеву.
    """
    flux: list[float] = []
    previous = 0.0
    for start in range(0, len(samples) - HOP, HOP):
        loudness = math.log1p(sum(map(abs, samples[start:start + HOP])) / HOP)
        # Только нарастание: спад громкости — это не удар, а его затухание.
        flux.append(max(0.0, loudness - previous))
        previous = loudness
    return flux


def _period(bpm: float) -> float:
    """Длина доли в кадрах огибающей."""
    return 60.0 / bpm * RATE / HOP


def _comb(flux: list[float], period: float) -> tuple[float, int]:
    """Складывает огибающую по кругу длиной в период.

    Возвращает силу лучшей фазы и её кадр. Сила делится на число оборотов:
    без этого длинный период всегда проигрывал бы короткому — просто потому,
    что в него попадает меньше ударов.
    """
    bins = [0.0] * (int(period) + 1)
    for index, value in enumerate(flux):
        if value:
            bins[int(index % period)] += value
    best = max(bins)
    return best / (len(flux) / period), bins.index(best)


def _best_bpm(flux: list[float], low: float, high: float, step: float) -> float:
    """Перебор темпов. При равном счёте побеждает первый, то есть медленный.

    Равный счёт — это не редкость, а свойство складки: у ровного бита период
    в две доли собирает вдвое меньше ударов на вдвое большем числе оборотов
    и даёт ровно тот же результат. Разбирать эту ничью незачем — шаг листания
    считается в долях и ложится на удары при любой из них.
    """
    best_score, best_bpm = -1.0, low
    bpm = low
    while bpm <= high + 1e-9:
        score = _comb(flux, _period(bpm))[0]
        if score > best_score:
            best_score, best_bpm = score, bpm
        bpm += step
    return best_bpm


def repeat(flux: list[float]) -> float:
    """Через сколько секунд огибающая повторяет сама себя.

    Автокорреляция: сдвигаем огибающую саму по себе и смотрим, при каком
    сдвиге она лучше всего на себя ложится. Среднее вычитается — иначе
    считалась бы громкость трека, одинаковая на любом сдвиге, а не сходство.
    Делим на число слагаемых: у большого сдвига их меньше, и без деления
    длинные периоды проигрывали бы коротким ни за что.
    """
    mean = sum(flux) / len(flux)
    centred = [value - mean for value in flux]
    frames_per_second = RATE / HOP
    low = int(PERIOD_LOW * frames_per_second)
    # Треть длины — чтобы сдвинутых друг на друга кусков было хотя бы три:
    # на двух совпадение случайно, на трёх уже нет.
    high = min(int(PERIOD_HIGH * frames_per_second), len(centred) // 3)
    best_score, best_lag = -math.inf, low
    for lag in range(low, high):
        score = sum(map(mul, centred, centred[lag:])) / (len(centred) - lag)
        if score > best_score:
            best_score, best_lag = score, lag
    return best_lag / frames_per_second


def detect(flux: list[float]) -> dict | None:
    """Темп, шаг листания и фаза по огибающей. None — мерить нечего."""
    if len(flux) < MIN_FRAMES:
        return None
    period = repeat(flux)

    # Доля — целый делитель периода: доля, которая его не делит, лежит мимо
    # музыки, как бы уверенно складка её ни называла. Из делителей, попавших
    # в человеческий диапазон, выбирает складка.
    levels = [60.0 * count / period for count in range(1, 33)]
    coarse = max(
        (bpm for bpm in levels if BPM_LOW <= bpm <= BPM_HIGH),
        key=lambda bpm: _comb(flux, _period(bpm))[0],
    )
    # Уточнение в два захода. Первый — широкий: период измерен с точностью
    # до кадра огибающей, и на коротком периоде это уже полтора процента.
    around = _best_bpm(flux, coarse * 0.985, coarse * 1.015, 0.05)
    bpm = _best_bpm(flux, around - 0.06, around + 0.06, 0.01)

    beat = 60.0 / bpm
    # Шаг листания — тот же период, поделённый пополам до трёх секунд:
    # деление периода остаётся на сетке музыки, а «около трёх секунд» — это
    # «успел рассмотреть, но не заскучал».
    flip = period
    while flip > TARGET_FLIP * 1.5:
        flip /= 2
    while flip < TARGET_FLIP / 1.5:
        flip *= 2
    beats = max(1, round(flip / beat))
    # Фаза ищется сразу на шаге листания, а не на доле: так снимок меняется
    # на самой сильной доле такта, а не на любой подвернувшейся.
    offset = _comb(flux, _period(bpm) * beats)[1] * HOP / RATE
    return {
        "bpm": round(bpm, 2),
        "flip_ms": round(beat * beats * 1000),
        "offset_ms": round(offset * 1000),
    }


def analyze(path: Path) -> dict | None:
    """Меряет темп файла. None — ffmpeg нет, файл не звук или он слишком мал."""
    samples = _pcm(path)
    if samples is None:
        return None
    return detect(envelope(samples))


def _tracks() -> list[dict]:
    """Что записано в плейлисте. Пусто — витрина молчит."""
    try:
        tracks = json.loads((config.MUSIC / LIST_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return tracks if isinstance(tracks, list) else []


def add(name: str, title: str, info: dict) -> int:
    """Добавляет скачанную песню в плейлист, отдаёт число песен в нём."""
    # Одиночный трек прежних версий витрина больше не читает, а место на диске
    # он занимает: первая же добавленная песня его и уносит.
    for old in config.MUSIC.glob("track.*"):
        old.unlink()
    tracks = [track for track in _tracks() if track.get("file") != name]
    tracks.append({"file": name, "title": title, **info})
    (config.MUSIC / LIST_FILE).write_text(
        json.dumps(tracks, ensure_ascii=False), encoding="utf-8"
    )
    return len(tracks)


def forget() -> None:
    """Убирает всю музыку магазина — в витрине становится тихо."""
    for old in config.MUSIC.glob("track*"):
        old.unlink()


def playlist() -> list[dict]:
    """Плейлист витрины: адрес и ритм каждой песни.

    Порядок здесь — порядок присылки, тасует его сама витрина и на каждом
    открытии заново. Метка версии в адресе: имена файлов повторяются от
    плейлиста к плейлисту, и без неё браузер играл бы старую песню из кэша.
    """
    tracks = []
    for track in _tracks():
        try:
            version = int((config.MUSIC / track["file"]).stat().st_mtime)
        except (OSError, KeyError, TypeError):
            continue  # файл унесли руками — песню просто не показываем
        tracks.append({**track, "url": f"/music/{track['file']}?v={version}"})
    return tracks


def _add_files(paths: list[Path]) -> int:
    """`--add`: кладёт песни в плейлист прямо с диска, минуя Telegram.

    Боту Telegram отдаёт файлы не больше 20 МБ, а альбом в хорошем качестве
    весит больше. Такой альбом проще положить рядом с базой руками — витрине
    всё равно, кто положил файл, лишь бы он был в плейлисте.
    """
    if not paths:
        print("нужны файлы: python -m src.music --add альбом/*.mp3")
        return 2
    config.MUSIC.mkdir(parents=True, exist_ok=True)
    for path in paths:
        if not path.is_file():
            print(f"{path}: нет такого файла")
            return 1
        info = analyze(path)
        suffix = path.suffix.lower()
        name = free_name(suffix if suffix in set(SUFFIX_BY_MIME.values()) else ".mp3")
        shutil.copyfile(path, config.MUSIC / name)
        count = add(name, path.stem, info or DEFAULT_FLIP)
        tempo = f"{info['bpm']} BPM" if info else "темп не определился"
        print(f"{count}. {path.stem} — {tempo}")
    return 0


def main(argv: list[str]) -> int:
    """`python -m src.music файл` — темп файла; `--add файлы` — в плейлист витрины."""
    if argv and argv[0] == "--add":
        return _add_files([Path(name) for name in argv[1:]])
    if not argv:
        print("нужен файл: python -m src.music track.mp3 (или --add альбом/*.mp3)")
        return 2
    info = analyze(Path(argv[0]))
    if not info:
        print("темп не определился: нет ffmpeg или это не музыка")
        return 1
    print(f"темп:      {info['bpm']} BPM")
    print(f"листание:  раз в {info['flip_ms'] / 1000:.2f} с")
    print(f"фаза:      {info['offset_ms']} мс от начала")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
