"""Очертания материков для планеты витрины — из данных Natural Earth.

Планета на логотипе не просто сетка: по ней тонко прорисованы берега. Рисовать
их руками нечем, а гонять карту в браузер целиком незачем — витрина показывает
шар размером с ноготь в шапке и с ладонь на заставке. Поэтому берега берутся
один раз здесь: контуры упрощаются до сотни-другой точек на материк и ложатся
в `webapp/land.js` целыми десятыми долями градуса.

    python3 tools/globe.py                 # скачать и пересобрать webapp/land.js
    python3 tools/globe.py land.geojson    # из своего файла

Проекция считается в витрине, а не здесь: шар крутится, и точка берега едет
по нему каждый кадр. Здесь только география, без единого пикселя.

Данные — ne_110m_land (public domain). Нужен только при смене карты; в боевых
зависимостях его нет, как и `fonttools` у `tools/wordmark.py`.
"""
import json
import math
import pathlib
import sys
import urllib.request

SOURCE = ("https://raw.githubusercontent.com/nvkelso/natural-earth-vector"
          "/master/geojson/ne_110m_land.geojson")
TOLERANCE = 0.55   # градусы: на чём берег перестаёт быть узнаваемым
MIN_SPAN = 3.0     # мелкие острова на ногте всё равно точка — выкидываем
SOUTH = -60.0      # Антарктиды на логотипе нет: срезаем по широте


def simplify(points, tol):
    """Дуглас — Пекер: убрать точки, которые не меняют формы берега."""
    if len(points) < 3:
        return points
    (x0, y0), (x1, y1) = points[0], points[-1]
    dx, dy = x1 - x0, y1 - y0
    span = math.hypot(dx, dy)
    far, worst = 0, 0.0
    for i, (x, y) in enumerate(points[1:-1], 1):
        if span:
            d = abs(dy * x - dx * y + x1 * y0 - y1 * x0) / span
        else:
            d = math.hypot(x - x0, y - y0)
        if d > worst:
            far, worst = i, d
    if worst <= tol:
        return [points[0], points[-1]]
    return simplify(points[:far + 1], tol)[:-1] + simplify(points[far:], tol)


def rings(data):
    for feature in data["features"]:
        geometry = feature["geometry"]
        polygons = (geometry["coordinates"] if geometry["type"] == "MultiPolygon"
                    else [geometry["coordinates"]])
        for polygon in polygons:
            for ring in polygon:
                yield ring


def main(source):
    if source.startswith("http"):
        with urllib.request.urlopen(source) as response:
            data = json.load(response)
    else:
        data = json.loads(pathlib.Path(source).read_text())

    out = []
    for ring in rings(data):
        # Кольцо замкнуто первой же точкой — держим замкнутым и после упрощения.
        line = simplify([(x, y) for x, y in ring], TOLERANCE)
        lons = [x for x, _ in line]
        lats = [y for _, y in line]
        if max(lats) < SOUTH:
            continue
        if max(lons) - min(lons) < MIN_SPAN and max(lats) - min(lats) < MIN_SPAN:
            continue
        out.append([v for point in line for v in
                    (round(point[0] * 10), round(point[1] * 10))])

    out.sort(key=len, reverse=True)
    text = ("// Сгенерировано `python3 tools/globe.py`. Руками не править.\n"
            "// Берега материков: замкнутые кольца, долгота и широта подряд,\n"
            "// в десятых долях градуса. Витрина сама кладёт их на шар.\n"
            "window.LAND = " + json.dumps(out, separators=(",", ":")) + ";\n")
    path = pathlib.Path(__file__).resolve().parent.parent / "webapp" / "land.js"
    path.write_text(text, encoding="utf-8")
    print("колец", len(out), "точек", sum(len(r) for r in out) // 2,
          "→", path, path.stat().st_size // 1024, "КБ")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else SOURCE)
