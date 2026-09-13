"""Единая точка конфигурации: пути, секреты, названия, доставка.

Файл `.env` разбирается своим кодом, а не библиотекой: формат «ключ=значение»
занимает двадцать строк, а `python-dotenv` — это ещё одна зависимость, которую
пришлось бы ставить и на сервере ради этих же двадцати строк.

Переменные окружения главнее файла: на VPS настройки задаёт юнит systemd,
и `.env` там может вообще не быть.

Проверить, что всё на месте, ничего не запуская:

    python -m src.config --check
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DATA = ROOT / "data"
PHOTOS = DATA / "photos"
# Присланный владельцем трек лежит в data/, а не в webapp/: выкладка
# затирает webapp/ целиком, и песня уезжала бы при каждом deploy.sh.
MUSIC = DATA / "music"
DB_FILE = DATA / "shop.db"
WEBAPP = ROOT / "webapp"


def _read_env_file(path: Path) -> dict[str, str]:
    """Разбирает `.env`: строки «ключ=значение», решётка — комментарий."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


@dataclass(frozen=True)
class Settings:
    """Настройки магазина. `problems` — список того, чего не хватает для запуска."""

    bot_token: str
    admin_ids: tuple[int, ...]
    webapp_url: str
    shop_name: str
    port: int
    delivery_options: tuple[str, ...]
    currency: str
    # Пускать в API запросы без подписи Telegram. Нужно, чтобы открывать витрину
    # в обычном браузере при отладке; на сервере включать нельзя — это дыра.
    dev_allow_unsigned: bool = False
    problems: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return not self.problems

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids


def load() -> Settings:
    """Собирает настройки и заодно копит список того, чего не хватает.

    Не падает на пустом токене: `--check` и разбор архива канала должны
    работать до того, как бот вообще заведён у @BotFather.
    """
    from_file = _read_env_file(ROOT / ".env")

    def value(key: str, default: str = "") -> str:
        return os.environ.get(key) or from_file.get(key) or default

    problems: list[str] = []

    token = value("TELEGRAM_BOT_TOKEN")
    if not token:
        problems.append("TELEGRAM_BOT_TOKEN пуст — токен берётся у @BotFather")

    admin_ids: list[int] = []
    for chunk in value("ADMIN_IDS").replace(" ", "").split(","):
        if chunk.isdigit():
            admin_ids.append(int(chunk))
        elif chunk:
            problems.append(f"ADMIN_IDS: «{chunk}» не похож на Telegram id")
    if not admin_ids:
        problems.append("ADMIN_IDS пуст — заявки будет некому получать (свой id даёт @userinfobot)")

    webapp_url = value("WEBAPP_URL").strip()
    if not webapp_url:
        problems.append("WEBAPP_URL пуст — кнопке «Открыть магазин» некуда вести")
    elif not webapp_url.startswith("https://"):
        # Telegram молча не открывает http-адреса, и снаружи это выглядит
        # как сломавшийся бот, а не как ошибка настройки.
        problems.append("WEBAPP_URL должен начинаться с https:// — Telegram не пускает в http")

    delivery = tuple(
        option.strip()
        for option in value("DELIVERY_OPTIONS", "Самовывоз;СДЭК;Почта России").split(";")
        if option.strip()
    )

    port_text = value("PORT", "8080")

    return Settings(
        bot_token=token,
        admin_ids=tuple(admin_ids),
        webapp_url=webapp_url,
        shop_name=value("SHOP_NAME", "Mco shop"),
        port=int(port_text) if port_text.isdigit() else 8080,
        delivery_options=delivery,
        currency=value("CURRENCY", "₽"),
        dev_allow_unsigned=value("DEV_ALLOW_UNSIGNED", "0") in {"1", "true", "yes"},
        problems=tuple(problems),
    )


def _check() -> int:
    settings = load()
    print(f"магазин:  {settings.shop_name}")
    print(f"витрина:  {settings.webapp_url or '—'}")
    print(f"порт:     {settings.port}")
    print(f"админы:   {', '.join(map(str, settings.admin_ids)) or '—'}")
    print(f"доставка: {', '.join(settings.delivery_options)}")
    print(f"токен:    {'задан' if settings.bot_token else '—'}")
    print(f"база:     {DB_FILE} {'(есть)' if DB_FILE.exists() else '(ещё нет)'}")
    if settings.problems:
        print("\nчего не хватает:")
        for problem in settings.problems:
            print(f"  — {problem}")
        return 1
    print("\nвсё на месте")
    return 0


if __name__ == "__main__":
    sys.exit(_check())
