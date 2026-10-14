"""Per-article model progress; display failures never interrupt an API request."""
import threading
import time

from .deepseek import DeepSeekClient


class ArticleProgress:
    def __init__(self, callback, details, interval=5.0):
        self.callback = callback
        self.details = details
        self.interval = interval
        self.started = time.monotonic()
        self.state = {"state": "waiting", "attempt": 1}
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.thread = None

    def emit(self, values):
        if self.callback is None:
            return
        event = {**self.details, **values, "elapsed_seconds": round(time.monotonic() - self.started, 1)}
        try:
            self.callback(event)
        except Exception:
            pass

    def notify(self, values):
        with self.lock:
            self.state = dict(values)
            self.emit(values)

    def __enter__(self):
        self.notify({"state": "started"})
        if self.callback is not None:
            self.thread = threading.Thread(target=self._heartbeat, daemon=True)
            self.thread.start()
        return self

    def _heartbeat(self):
        while not self.stop.wait(self.interval):
            with self.lock:
                values = dict(self.state)
                if values.get("state") != "retry":
                    values["state"] = "waiting"
                self.emit(values)

    def __exit__(self, *args):
        self.stop.set()
        if self.thread is not None:
            self.thread.join()


def complete_with_progress(client, *, callback, details, system_prompt, user_prompt):
    with ArticleProgress(callback, details) as activity:
        kwargs = {"system_prompt": system_prompt, "user_prompt": user_prompt}
        if isinstance(client, DeepSeekClient):
            kwargs["progress"] = activity.notify
        result = client.complete_json(**kwargs)
    return result, round(time.monotonic() - activity.started, 1)
