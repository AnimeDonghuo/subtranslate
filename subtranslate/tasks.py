"""Live, in-memory status for queued and running translation tasks.

Task numbers are per process, like the single-worker queue itself: a restart
clears them. Nothing here is persisted and no subtitle contents are kept.
"""

import asyncio
import html
from dataclasses import dataclass, field

from .progress import step_details

BAR_CELLS = 10
QUEUED = "queued"
RUNNING = "running"


def bar(completed: int, total: int, width: int = BAR_CELLS) -> str:
    """A stage bar: filled cells show completed stages, never a percentage."""
    filled = min(width, max(0, completed * width // max(1, total)))
    return "■" * filled + "□" * (width - filled)


def format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def person_line(task: "Task") -> str:
    """The sender in one readable line: name, numeric ID and @username."""
    name = html.escape(task.user_name or "Unknown name", quote=False)
    parts = [f"👤 {name}", f"🆔 <code>{task.user_id or 'unknown'}</code>"]
    if task.username:
        parts.append(f"@{html.escape(task.username, quote=False)}")
    return " · ".join(parts)


@dataclass
class Task:
    id: int
    job_key: tuple[int, int, str]
    chat_id: int
    user_id: int | None
    user_name: str = ""
    username: str = ""
    file_name: str = ""
    language: str = ""
    is_batch: bool = False
    batch_total: int = 0
    batch_current: int = 0
    batch_completed: int = 0
    queued_at: float = 0.0
    started_at: float = 0.0
    state: str = QUEUED
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    progress: object | None = None

    @property
    def stage(self) -> str:
        return getattr(self.progress, "stage", "downloading the original")

    @property
    def language_label(self) -> str:
        if self.is_batch:
            current = getattr(self.progress, "language", "")
            return f"Batch of {self.batch_total} languages" + (f" · current: {current}" if current else "")
        return self.language


class TaskRegistry:
    """Track the queued and running jobs so /status and /cancel_id can see them."""

    def __init__(self) -> None:
        self._tasks: dict[int, Task] = {}
        self._next_id = 0

    def register(self, *, job_key: tuple[int, int, str], chat_id: int, user_id: int | None,
                 user_name: str = "", username: str = "", file_name: str = "", language: str = "",
                 is_batch: bool = False, batch_total: int = 0, now: float = 0.0) -> Task:
        self._next_id += 1
        task = Task(
            id=self._next_id,
            job_key=job_key,
            chat_id=chat_id,
            user_id=user_id,
            user_name=user_name,
            username=username,
            file_name=file_name,
            language=language,
            is_batch=is_batch,
            batch_total=batch_total,
            queued_at=now,
        )
        self._tasks[task.id] = task
        return task

    def get(self, task_id: int) -> Task | None:
        return self._tasks.get(task_id)

    def remove(self, task_id: int) -> None:
        self._tasks.pop(task_id, None)

    def all(self) -> list[Task]:
        return [self._tasks[key] for key in sorted(self._tasks)]

    def for_chat(self, chat_id: int) -> list[Task]:
        return [task for task in self.all() if task.chat_id == chat_id]

    def mark_running(self, task_id: int, cancel: asyncio.Event, now: float = 0.0,
                     progress: object | None = None) -> Task | None:
        task = self._tasks.get(task_id)
        if task is None:
            return None
        task.state = RUNNING
        task.cancel = cancel
        task.started_at = now or task.queued_at
        if progress is not None:
            task.progress = progress
        return task

    def attach_progress(self, task_id: int, progress: object) -> None:
        task = self._tasks.get(task_id)
        if task is not None:
            task.progress = progress

    def request_cancel(self, task_id: int, *, user_id: int | None, owner: bool) -> tuple[str, Task | None]:
        """Return (outcome, task): missing, forbidden, queued, running or stale."""
        task = self._tasks.get(task_id)
        if task is None:
            return "missing", None
        if not owner and task.user_id != user_id:
            return "forbidden", task
        if task.state == QUEUED:
            return "queued", task
        task.cancel.set()
        return "running", task


def render_task(task: Task, *, number: int | None = None, now: float | None = None) -> str:
    """One task block for /status; the bar shows stages, not text progress."""
    head = f"{number}. " if number else ""
    lines = [f"{head}🔄 <b>Task #{task.id}</b> · translation in progress" if task.state == RUNNING
             else f"{head}🕓 <b>Task #{task.id}</b> · waiting in queue"]
    lines.append(person_line(task))
    if task.file_name:
        lines.append(f"📄 {html.escape(task.file_name, quote=False)}")
    if task.state == QUEUED:
        lines.extend(("", f"Language: {html.escape(task.language_label, quote=False)}",
                      f"Queued behind another translation. Use <code>/cancel_id {task.id}</code> to remove it."))
        return "\n".join(lines)
    elapsed = format_elapsed((now if now is not None else asyncio.get_running_loop().time())
                             - (task.started_at or task.queued_at))
    step, steps, label = step_details(task.stage)
    lines.append("")
    if task.is_batch:
        completed = getattr(task.progress, "batch_completed", 0) if task.progress else 0
        lines.extend((f"Batch: {completed}/{task.batch_total} languages done",
                      f"[{bar(completed, task.batch_total)}]", ""))
    current = getattr(task.progress, "language", "") if task.progress else ""
    lines.extend((f"Translating to {html.escape(current or task.language, quote=False)}",
                  f"[{bar(step, steps)}] Step {step}/{steps}",
                  f"Stage: {label}",
                  f"Elapsed: {elapsed}",
                  "",
                  "The bar shows stages, not the website's translation percentage."))
    return "\n".join(lines)
