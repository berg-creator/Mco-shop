"""Заглушки Telegram для тестов обработчиков.

Обработчики бота и админки — обычные функции: им приходит сообщение или
нажатие кнопки, они зовут `answer` и `edit_text`. Поднимать ради этого
диспетчер и живой Bot API незачем — нужен объект с теми же тремя методами,
который запоминает, что ему сказали, и умеет падать по требованию: половина
кода в боте написана именно про то, что Telegram может не ответить.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage


class ФейкПользователь:
    def __init__(self, user_id: int = 1, username: str = "tester") -> None:
        self.id = user_id
        self.username = username
        self.first_name = "Тест"


class ФейкФайл:
    def __init__(self, file_id: str = "AgACfoto") -> None:
        self.file_id = file_id


class ФейкСообщение:
    """Сообщение, которое запоминает ответы вместо отправки в Telegram."""

    def __init__(
        self,
        text: str = "",
        user_id: int = 1,
        photo: list[ФейкФайл] | None = None,
        edit_fails: bool = False,
    ) -> None:
        self.text = text
        self.from_user = ФейкПользователь(user_id)
        self.photo = photo or []
        self.edit_fails = edit_fails
        self.answers: list[tuple[str, Any]] = []
        self.edits: list[tuple[str, Any]] = []

    async def answer(self, text: str, reply_markup: Any = None, **kwargs: Any) -> "ФейкСообщение":
        self.answers.append((text, reply_markup))
        return self

    async def edit_text(self, text: str, reply_markup: Any = None, **kwargs: Any) -> "ФейкСообщение":
        if self.edit_fails:
            # Сообщение старше двух суток: Telegram отказывается его править.
            raise RuntimeError("message can't be edited")
        self.edits.append((text, reply_markup))
        return self

    @property
    def последний_ответ(self) -> str:
        return self.answers[-1][0] if self.answers else ""


class ФейкКнопка:
    """Нажатие инлайн-кнопки."""

    def __init__(
        self,
        data: str,
        user_id: int = 1,
        message: ФейкСообщение | None = None,
    ) -> None:
        self.data = data
        self.from_user = ФейкПользователь(user_id)
        self.message = message if message is not None else ФейкСообщение()
        self.answers: list[tuple[str, bool]] = []

    async def answer(self, text: str = "", show_alert: bool = False, **kwargs: Any) -> None:
        self.answers.append((text, show_alert))

    @property
    def последний_ответ(self) -> str:
        return self.answers[-1][0] if self.answers else ""


class ФейкБот:
    """Bot API без сети. `падает=True` — Telegram недоступен."""

    def __init__(self, падает: bool = False) -> None:
        self.падает = падает
        self.sent: list[tuple[int, str]] = []
        self.menu_button: Any = None
        self.downloaded: list[str] = []

    async def send_message(self, chat_id: int, text: str, reply_markup: Any = None, **kw: Any) -> None:
        if self.падает:
            raise RuntimeError("bot was blocked by the user")
        self.sent.append((chat_id, text))

    async def set_chat_menu_button(self, menu_button: Any = None, **kwargs: Any) -> None:
        self.menu_button = menu_button

    async def download(self, file_id: str, destination: Any = None) -> None:
        if self.падает:
            raise RuntimeError("file is too big")
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xff\xd8\xff")  # три байта заголовка jpeg
        self.downloaded.append(file_id)


def состояние() -> FSMContext:
    """Живой FSMContext на памяти: мастер добавления товара опирается на него."""
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=0, chat_id=1, user_id=1),
    )
