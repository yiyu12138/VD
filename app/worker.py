"""简单的可调并发下载队列：任务排队执行，避免同时启动过多 yt-dlp 进程。"""
import queue
import threading
from collections.abc import Callable


class JobQueue:
    def __init__(self, runner: Callable[[str], None], workers: int = 2):
        self._runner = runner
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._pending: set[str] = set()
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._target = 0
        self.resize(workers)

    def submit(self, job_id: str) -> None:
        with self._lock:
            if job_id in self._pending:
                return
            self._pending.add(job_id)
        self._queue.put(job_id)

    def resize(self, workers: int) -> None:
        workers = max(1, min(6, int(workers)))
        with self._lock:
            self._threads = [t for t in self._threads if t.is_alive()]
            diff = workers - self._target
            self._target = workers
        if diff > 0:
            for _ in range(diff):
                thread = threading.Thread(target=self._loop, daemon=True)
                thread.start()
                self._threads.append(thread)
        else:
            for _ in range(-diff):
                self._queue.put(None)

    def shutdown(self) -> None:
        for _ in range(self._target):
            self._queue.put(None)

    def _loop(self) -> None:
        while True:
            job_id = self._queue.get()
            if job_id is None:
                return
            with self._lock:
                self._pending.discard(job_id)
            try:
                self._runner(job_id)
            except Exception:
                pass
