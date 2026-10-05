"""Terminal progress with real step counts and indeterminate activity/elapsed time."""
from contextlib import contextmanager
import os
import sys
import threading
import time


def size_label(count):
    value = float(count)
    for unit in ('B', 'KiB', 'MiB', 'GiB'):
        if value < 1024 or unit == 'GiB':
            return f'{value:.1f} {unit}'
        value /= 1024


class Progress:
    def __init__(self, title, total, stream=None, interval=0.25):
        self.title, self.total = title, total
        self.stream = stream if stream is not None else sys.stdout
        self.animated = bool(getattr(self.stream, 'isatty', lambda: False)()) and os.environ.get('TERM') != 'dumb'
        self.interval = interval
        self.completed = 0
        self.label = ''
        self.detail = ''
        self.started = time.monotonic()
        self._lock = threading.Lock()
        self._frame = 0

    def download(self, received, total=None):
        with self._lock:
            if total and total > 0:
                percent = min(100, int(received * 100 / total))
                self.detail = f'下载 {percent}% · {size_label(received)} / {size_label(total)}'
            else:
                self.detail = f'已下载 {size_label(received)}'

    def _render(self, status, *, final=False):
        with self._lock:
            filled = self.completed * 20 // self.total
            bar = '=' * filled + '-' * (20 - filled)
            elapsed = max(0, int(time.monotonic() - self.started))
            marker = ('·', '··', '···')[self._frame % 3] if not final else status
            self._frame += 1
            detail = f' | {self.detail}' if self.detail else ''
            line = (f'{self.title} [{bar}] 已完成 {self.completed}/{self.total} 步 '
                    f'| {self.label} {marker}{detail} | 已用 {elapsed}s')
            prefix = '\r\033[2K' if self.animated else ''
            suffix = '\n' if final or not self.animated else ''
            self.stream.write(prefix + line + suffix)
            self.stream.flush()

    def _animate(self, stop):
        while not stop.wait(self.interval):
            self._render('进行中')

    @contextmanager
    def step(self, label):
        self.label, self.detail = label, ''
        stop = threading.Event()
        worker = None
        self._render('进行中')
        if self.animated:
            worker = threading.Thread(target=self._animate, args=(stop,), daemon=True)
            worker.start()
        try:
            yield self
        except BaseException:
            stop.set()
            if worker:
                worker.join()
            self._render('未完成', final=True)
            raise
        else:
            stop.set()
            if worker:
                worker.join()
            self.completed += 1
            self._render('完成', final=True)
