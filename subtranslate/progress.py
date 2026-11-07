"""A single, rate-limited Telegram status message for a translation job."""

import asyncio
import logging

from telegram.error import TelegramError

logger = logging.getLogger(__name__)

# These are processing stages, not percentages of translated subtitle text.
STAGES = (
    ("downloading the original", "Downloading your subtitle from Telegram"),
    ("starting the browser", "Preparing the browser"),
    ("opening the subtitle site", "Opening the subtitle website"),
    ("uploading and parsing the subtitle", "Uploading and checking the subtitle"),
    ("selecting the target language", "Selecting the target language"),
    ("translating", "Translating on the website"),
    ("waiting for translation to settle", "Waiting for the website to finish"),
    ("downloading", "Downloading the website's file"),
    ("validating the downloaded file", "Checking cues and timestamps"),
    ("sending result", "Sending the file to Telegram"),
)
_STAGE_INFO = {name: (index, label) for index, (name, label) in enumerate(STAGES, start=1)}


def _bar(completed: int, total: int, width: int = 10) -> str:
    filled = min(width, max(0, completed * width // max(1, total)))
    return "■" * filled + "□" * (width - filled)


class ProgressMessage:
    """Coalesce stages so Telegram edits never slow down browser operations."""

    def __init__(self, bot, chat_id: int, language: str, *, batch_total: int = 0,
                 edit_interval: float = 1.0, heartbeat_interval: float = 20.0,
                 edit_timeout: float = 4.0) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.language = language
        self.batch_total = batch_total
        self.batch_completed = 0
        self.batch_current = 0
        self.part_current = 0
        self.part_total = 0
        self.stage = "downloading the original"
        self.edit_interval = edit_interval
        self.heartbeat_interval = heartbeat_interval
        self.edit_timeout = edit_timeout
        self.started_at = asyncio.get_running_loop().time()
        self.message_id: int | None = None
        self._event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._last_text = ""
        self._last_edit = self.started_at
        self._terminal: str | None = None
        self._terminal_sent = False

    def render(self) -> str:
        index, label = _STAGE_INFO.get(self.stage, (1, "Preparing the translation"))
        elapsed = int(asyncio.get_running_loop().time() - self.started_at)
        lines = []
        if self.batch_total:
            lines.append(f"Batch: {self.batch_completed}/{self.batch_total} languages processed")
            lines.append(f"[{_bar(self.batch_completed, self.batch_total)}]")
            if self.batch_current:
                lines.append(f"Current: {self.language} ({self.batch_current}/{self.batch_total})")
        else:
            lines.append(f"Translating to {self.language}")
        if self.part_total > 1:
            lines.append(f"Subtitle part: {self.part_current}/{self.part_total} (processed sequentially)")
        lines.extend((
            f"[{_bar(index, len(STAGES))}] Step {index}/{len(STAGES)}",
            f"Stage: {label}",
            f"Elapsed: {elapsed // 60}:{elapsed % 60:02d}",
            "The bar shows stages, not the website's translation percentage.",
        ))
        return "\n".join(lines)

    @classmethod
    async def start(cls, bot, chat_id: int, language: str, *, batch_total: int = 0, **kwargs):
        progress = cls(bot, chat_id, language, batch_total=batch_total, **kwargs)
        initial = progress.render()
        if batch_total:
            initial = f"Starting {batch_total} languages, one at a time. Use /cancel to stop.\n\n{initial}"
        try:
            message = await bot.send_message(chat_id=chat_id, text=initial)
        except TelegramError as exc:
            logger.warning("Could not send a Telegram status (%s)", type(exc).__name__)
            return None
        progress.message_id = message.message_id
        progress._last_text = initial
        progress._last_edit = asyncio.get_running_loop().time()
        progress._task = asyncio.create_task(progress._run(), name="translation-progress")
        return progress

    def set_stage(self, stage: str) -> None:
        if self._terminal is None:
            if stage.startswith("part:"):
                # Internal progress events carry only bounded numeric counts.
                try:
                    current, total = map(int, stage[5:].split("/", 1))
                except ValueError:
                    return
                if not 1 <= current <= total:
                    return
                self.part_current, self.part_total = current, total
            else:
                self.stage = stage
            self._event.set()

    def set_language(self, name: str, current: int, completed: int) -> None:
        if self._terminal is None:
            self.language = name
            self.batch_current = current
            self.batch_completed = completed
            self.part_current = self.part_total = 0
            self._event.set()

    def set_completed(self, completed: int) -> None:
        if self._terminal is None:
            self.batch_completed = completed
            self._event.set()

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._event.wait(), timeout=self.heartbeat_interval)
            except TimeoutError:
                # Keep long website waits visibly alive without claiming an ETA.
                if self.stage not in {"translating", "waiting for translation to settle"}:
                    continue
            delay = self.edit_interval - (asyncio.get_running_loop().time() - self._last_edit)
            if delay > 0:
                await asyncio.sleep(delay)
            self._event.clear()
            terminal = self._terminal is not None
            text = self._terminal if terminal else self.render()
            sent = text == self._last_text
            if not sent:
                try:
                    async with asyncio.timeout(self.edit_timeout):
                        await self.bot.edit_message_text(chat_id=self.chat_id, message_id=self.message_id, text=text)
                    sent = True
                    self._last_text = text
                except Exception as exc:  # noqa: BLE001 - progress is best-effort, never log request details
                    logger.warning("Could not edit Telegram status (%s)", type(exc).__name__)
                self._last_edit = asyncio.get_running_loop().time()
            if terminal:
                self._terminal_sent = sent
                return

    async def finish(self, text: str) -> bool:
        """Try to replace the status with a final result, without waiting indefinitely."""
        self._terminal = text
        self._event.set()
        if self._task is None:
            return False
        try:
            await asyncio.wait_for(self._task, timeout=self.edit_timeout + self.edit_interval + 1)
        except TimeoutError:
            logger.warning("Telegram status update timed out")
        except Exception as exc:  # noqa: BLE001 - never let status reporting fail a job
            logger.warning("Telegram status update failed (%s)", type(exc).__name__)
        return self._terminal_sent

    async def close(self) -> None:
        if self._task is not None:
            if not self._task.done():
                self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
