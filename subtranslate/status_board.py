"""One live /status message per chat, refreshed on a fixed interval.

Each chat owns its own message, so refreshing or cancelling in one user's
status never touches another user's. Starting /status again deletes the old
message and shows a fresh one.
"""

import asyncio
import logging

from telegram.error import BadRequest, RetryAfter, TelegramError

from .tasks import render_board, status_keyboard

logger = logging.getLogger(__name__)

# How often the live status edits itself while something is running.
REFRESH_SECONDS = 6.0


def _retry_seconds(exc: RetryAfter) -> float:
    value = getattr(exc, "retry_after", 1)
    return float(value.total_seconds()) if hasattr(value, "total_seconds") else float(value)


class StatusBoard:
    def __init__(self, service, chat_id: int, *, viewer_id: int,
                 interval: float = REFRESH_SECONDS) -> None:
        self.service = service
        self.chat_id = chat_id
        self.viewer_id = viewer_id
        self.interval = interval
        self.message_id: int | None = None
        self._task: asyncio.Task | None = None
        self._last_text = ""

    def tasks(self):
        if self.service._is_owner(self.viewer_id):
            return self.service.tasks.all()
        return self.service.tasks.for_chat(self.chat_id)

    def render(self, tasks=None) -> str:
        tasks = self.tasks() if tasks is None else tasks
        return render_board(tasks, owner=self.service._is_owner(self.viewer_id),
                            interval=int(self.interval))

    async def show(self) -> bool:
        """Send a new live message for this chat and start refreshing it."""
        tasks = self.tasks()
        text = self.render(tasks)
        try:
            message = await self.service.bot.send_message(
                chat_id=self.chat_id, text=text, parse_mode="HTML",
                reply_markup=status_keyboard(tasks), disable_web_page_preview=True,
            )
        except TelegramError as exc:
            logger.warning("Could not send the live status (%s)", type(exc).__name__)
            return False
        self.message_id = message.message_id
        self._last_text = text
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="live-status")
        return True

    async def refresh(self) -> None:
        """Redraw immediately (the manual Refresh button)."""
        await self._edit(force=True)

    async def _edit(self, *, force: bool = False) -> None:
        if self.message_id is None:
            return
        tasks = self.tasks()
        text = self.render(tasks)
        if not force and text == self._last_text:
            return
        try:
            await self.service.bot.edit_message_text(
                chat_id=self.chat_id, message_id=self.message_id, text=text,
                parse_mode="HTML", reply_markup=status_keyboard(tasks),
                disable_web_page_preview=True,
            )
            self._last_text = text
        except RetryAfter as exc:
            # Telegram's flood wait: pause, then retry this edit once.
            delay = min(60.0, _retry_seconds(exc) + 0.5)
            logger.info("Live status flood wait: pausing %.0f seconds", delay)
            await asyncio.sleep(delay)
            try:
                await self.service.bot.edit_message_text(
                    chat_id=self.chat_id, message_id=self.message_id, text=text,
                    parse_mode="HTML", reply_markup=status_keyboard(tasks),
                    disable_web_page_preview=True,
                )
                self._last_text = text
            except TelegramError as retry_exc:
                logger.info("Live status retry failed (%s)", type(retry_exc).__name__)
        except BadRequest:
            pass  # Usually "message is not modified" after a duplicate edit.
        except TelegramError as exc:
            logger.info("Could not edit the live status (%s)", type(exc).__name__)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            tasks = self.tasks()
            if not tasks:
                # Everything finished: one last edit, then stop editing.
                self._last_text = ""
                await self._edit(force=True)
                return
            await self._edit()

    async def delete(self) -> None:
        """Remove the previous live message so only the newest one survives."""
        message_id, self.message_id = self.message_id, None
        if message_id is None or self.service.bot is None:
            return
        try:
            await self.service.bot.delete_message(chat_id=self.chat_id, message_id=message_id)
        except TelegramError as exc:
            logger.info("Could not delete the old live status (%s)", type(exc).__name__)

    async def close(self) -> None:
        if self._task is not None:
            if not self._task.done():
                self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None


class StatusBoards:
    """One board per chat, so users never share or overwrite each other."""

    def __init__(self) -> None:
        self._boards: dict[int, StatusBoard] = {}

    def get(self, chat_id: int) -> StatusBoard | None:
        return self._boards.get(chat_id)

    async def show(self, service, chat_id: int, viewer_id: int) -> bool:
        previous = self._boards.pop(chat_id, None)
        if previous is not None:
            await previous.close()
            await previous.delete()
        board = StatusBoard(service, chat_id, viewer_id=viewer_id)
        shown = await board.show()
        if shown:
            self._boards[chat_id] = board
        return shown

    async def close_all(self) -> None:
        boards, self._boards = list(self._boards.values()), {}
        for board in boards:
            await board.close()
