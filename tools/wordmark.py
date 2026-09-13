"""Контуры имени магазина двумя шрифтами — для превращения букв на заставке.

Заставка обязана показать, как буквы гротеска сами становятся росчерком: не
подмена надписи, не проявление поверх, а одна форма, перетекающая в другую.
Шрифт в шрифт браузер перетекать не умеет, поэтому буквы на время превращения
становятся кривыми, и кривая едет в кривую точка в точку.

Скрипт разбирает два системных шрифта (гротеск витрины и каллиграфию логотипа),
раскладывает имя магазина каждым из них, разбивает буквы на равное число точек
и кладёт результат в `webapp/wordmark.js`. Запускается руками и только на маке,
где эти шрифты есть; витрине он не нужен — она читает готовый файл.

    python3 tools/wordmark.py "M co."

Точек на контур берём с запасом (кривая из ста двадцати отрезков на экране
неотличима от кривой), а соответствие контуров ищем перебором сдвига: так
дырка в «о» едет в дырку, а не в наружный обвод.
"""
import json
import math
import pathlib
import sys

from fontTools.pens.basePen import BasePen
from fontTools.ttLib import TTFont, TTCollection
from fontTools.varLib import instancer

SANS = "/System/Library/Fonts/SFNS.ttf"            # -apple-system витрины
SCRIPT = "/System/Library/Fonts/Supplemental/SnellRoundhand.ttc"
POINTS = 140        # точек на контур в готовом файле
DENSE = 220         # точек, по которым ищется соответствие
STEPS = 24          # на сколько отрезков дробится кривая Безье
# Разрядка табло (`letter-spacing` у `#boot-brand` в styles.css) вносится
# в раскладку гротеска: буквы должны встать ровно туда, где их нарисовал
# браузер, иначе в момент подмены текста кривыми они дёрнутся.
TRACKING = 0.14


class FlatPen(BasePen):
    """Пен, который сразу дробит кривые в ломаные: морфить проще точки."""

    def __init__(self, glyphSet):
        super().__init__(glyphSet)
        self.contours = []
        self._current = []

    def _moveTo(self, pt):
        self._flush()
        self._current = [pt]

    def _lineTo(self, pt):
        self._current.append(pt)

    def _curveToOne(self, p1, p2, p3):
        p0 = self._current[-1]
        for i in range(1, STEPS + 1):
            t = i / STEPS
            u = 1 - t
            self._current.append((
                u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0],
                u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1],
            ))

    def _closePath(self):
        self._flush()

    def _endPath(self):
        self._flush()

    def _flush(self):
        if len(self._current) > 2:
            self.contours.append(self._current)
        self._current = []


def area(contour):
    s = 0.0
    for (x0, y0), (x1, y1) in zip(contour, contour[1:] + contour[:1]):
        s += x0 * y1 - x1 * y0
    return s / 2


def resample(contour, count=DENSE):
    """Равномерно по длине: только так точки двух букв соответствуют друг другу."""
    pts = contour + [contour[0]]
    lengths = [0.0]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        lengths.append(lengths[-1] + math.hypot(x1 - x0, y1 - y0))
    total = lengths[-1]
    out, j = [], 0
    for i in range(count):
        target = total * i / count
        while j < len(lengths) - 2 and lengths[j + 1] < target:
            j += 1
        span = lengths[j + 1] - lengths[j] or 1
        t = (target - lengths[j]) / span
        (x0, y0), (x1, y1) = pts[j], pts[j + 1]
        out.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
    return out


def align(src, dst):
    """Сдвиг начала контура, при котором точки едут по кратчайшему пути."""
    def box(pts):
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return min(xs), min(ys), max(xs) - min(xs) or 1, max(ys) - min(ys) or 1

    ax, ay, aw, ah = box(src)
    bx, by, bw, bh = box(dst)
    na = [((x - ax) / aw, (y - ay) / ah) for x, y in src]
    nb = [((x - bx) / bw, (y - by) / bh) for x, y in dst]
    best, shift = None, 0
    for k in range(len(nb)):
        s = sum((na[i][0] - nb[(i + k) % len(nb)][0]) ** 2
                + (na[i][1] - nb[(i + k) % len(nb)][1]) ** 2 for i in range(0, len(na), 4))
        if best is None or s < best:
            best, shift = s, k
    return dst[shift:] + dst[:shift]


def unit(pts):
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    x0, y0 = min(xs), min(ys)
    w = (max(xs) - x0) or 1.0
    h = (max(ys) - y0) or 1.0
    return [((x - x0) / w, (y - y0) / h) for x, y in pts]


def warp(src, dst, count=POINTS):
    """Точка к точке по пути наименьшей стоимости, не нарушая порядка обхода.

    Равномерно по длине две буквы не связываются: у росчерка есть хвост,
    которому в гротеске не соответствует ничего, и его точки уезжают через всю
    букву. На первых же кадрах «M» рвало заусенцем и теряло угол — глазу это
    и читалось как «буква резко сменилась», хотя прошла сотая часть пути.
    Монотонный путь (DTW) выращивает длинный хвост из короткого куска, а всё
    остальное ведёт напрямую, и промежуточные формы остаются буквами.
    """
    a, b = unit(src), unit(dst)
    n, m = len(a), len(b)
    inf = float("inf")
    d = [[inf] * (m + 1) for _ in range(n + 1)]
    d[0][0] = 0.0
    for i in range(1, n + 1):
        ax, ay = a[i - 1]
        row, prev = d[i], d[i - 1]
        for j in range(1, m + 1):
            bx, by = b[j - 1]
            row[j] = (ax - bx) ** 2 + (ay - by) ** 2 + min(prev[j], row[j - 1], prev[j - 1])
    path, i, j = [], n, m
    while i and j:
        path.append((i - 1, j - 1))
        best = min(d[i - 1][j - 1], d[i - 1][j], d[i][j - 1])
        if best == d[i - 1][j - 1]:
            i, j = i - 1, j - 1
        elif best == d[i - 1][j]:
            i -= 1
        else:
            j -= 1
    path.reverse()
    step = (len(path) - 1) / (count - 1)
    picks = [path[round(k * step)] for k in range(count)]
    return [src[i] for i, _ in picks], [dst[j] for _, j in picks]


def glyphs(font, word, upm, tracking=0.0):
    """Слово, разложенное шрифтом: контуры каждой буквы в единицах кегля."""
    cmap = font.getBestCmap()
    gs = font.getGlyphSet()
    hmtx = font["hmtx"]
    out, pen_x = [], 0.0
    for ch in word:
        name = cmap.get(ord(ch))
        pen = FlatPen(gs)
        if name:
            gs[name].draw(pen)
        contours = []
        for c in pen.contours:
            c = resample(c)
            if area(c) < 0:            # одинаковый обход у всех контуров
                c = c[::-1]
            contours.append([(x / upm + pen_x, y / upm) for x, y in c])
        out.append({"char": ch, "origin": pen_x, "contours": contours})
        pen_x += (hmtx[name][0] if name else upm * 0.3) / upm + tracking
        pen_x *= 1  # pen_x уже в единицах кегля
    return out


def pair(a, b):
    """Контуры буквы к контурам той же буквы другим шрифтом."""
    a = sorted(a, key=lambda c: -abs(area(c)))
    b = sorted(b, key=lambda c: -abs(area(c)))
    while len(a) < len(b):             # лишнему контуру расти из точки
        a.append(shrink(b[len(a)]))
    while len(b) < len(a):
        b.append(shrink(a[len(b)]))
    out = []
    for i, (x, y) in enumerate(zip(a, b)):
        if i:
            # Дырка обходится в обратную сторону: с ней правило `nonzero`
            # вырезает её, а не заливает. Правило важно именно в середине
            # превращения — форма там сама себя пересекает, и `evenodd`
            # прорезал в букве белые щели.
            x, y = x[::-1], y[::-1]
        out.append(warp(x, align(x, y)))
    return out


def shrink(contour):
    cx = sum(p[0] for p in contour) / len(contour)
    cy = sum(p[1] for p in contour) / len(contour)
    return [(cx, cy)] * len(contour)


def box(letters, key):
    """Габарит чернил слова: по нему заставка ставит кривые на место текста."""
    xs = [v for l in letters for c in l[key] for v in c[0::2]]
    ys = [v for l in letters for c in l[key] for v in c[1::2]]
    return [round(min(xs), 4), round(min(ys), 4), round(max(xs), 4), round(max(ys), 4)]


def main(word):
    sans = instancer.instantiateVariableFont(TTFont(SANS), {"wght": 700, "opsz": 20})
    script = None
    for f in TTCollection(SCRIPT).fonts:
        if "Bold" in f["name"].getDebugName(4):
            script = f
    script = script or TTCollection(SCRIPT).fonts[0]

    a = glyphs(sans, word, sans["head"].unitsPerEm, TRACKING)
    b = glyphs(script, word, script["head"].unitsPerEm)
    print("контуры:", [(l["char"], len(l["contours"]), len(r["contours"])) for l, r in zip(a, b)])

    letters = []
    for at, (l, r) in enumerate(zip(a, b)):
        pairs = pair(l["contours"], r["contours"])
        if not pairs:
            continue                      # пробел рисовать нечем
        # `at` и `x` — чтобы витрина поставила кривую ровно туда, где браузер
        # нарисовал эту букву: `at` — её место в слове (буквы без контуров сюда
        # не попадают, и счёт сбивается), `x` — перо в единицах кегля. Разрядку
        # и ширины браузер считает по своему оптическому размеру, а он зависит
        # от кегля, то есть от экрана: сойтись с ним можно только на месте.
        letters.append({
            "at": at,
            "x": round(l["origin"], 4),
            "from": [[round(v, 4) for p in c for v in p] for c, _ in pairs],
            "to": [[round(v, 4) for p in c for v in p] for _, c in pairs],
        })
    data = {
        "word": word,
        "points": POINTS,
        "tracking": TRACKING,
        "from": box(letters, "from"),
        "to": box(letters, "to"),
        "letters": letters,
    }
    out = pathlib.Path(__file__).resolve().parent.parent / "webapp" / "wordmark.js"
    out.write_text(
        "// Сгенерировано `python3 tools/wordmark.py`. Руками не править.\n"
        "// Контуры имени магазина двумя шрифтами: гротеском витрины и росчерком\n"
        "// логотипа, точка в точку. Заставка перегоняет одни в другие — так буквы\n"
        "// превращаются в другой шрифт, а не сменяются им.\n"
        "window.WORDMARK = " + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + ";\n",
        encoding="utf-8")
    print("записано", out, out.stat().st_size // 1024, "КБ")
    return data


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "M co.")
