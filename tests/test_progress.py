import io
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from progress import Progress


class ProgressTests(unittest.TestCase):
    def test_plain_output_has_real_step_counts_and_no_terminal_escapes(self):
        out = io.StringIO()
        progress = Progress('安装', 2, stream=out)
        with progress.step('下载'):
            progress.download(25, 100)
        with progress.step('执行安装器'):
            pass
        text = out.getvalue()
        self.assertIn('下载 25%', text)
        self.assertIn('已完成 2/2 步', text)
        self.assertNotIn('\033', text)
        self.assertNotIn('\r', text)

    def test_unknown_download_size_has_bytes_without_fake_percentage(self):
        out = io.StringIO()
        progress = Progress('安装', 1, stream=out)
        with progress.step('下载'):
            progress.download(2048)
        self.assertIn('已下载 2.0 KiB', out.getvalue())
        self.assertNotIn('%', out.getvalue())

    def test_failure_does_not_complete_the_step_or_print_exception_contents(self):
        out = io.StringIO()
        progress = Progress('安装', 1, stream=out)
        with self.assertRaises(RuntimeError):
            with progress.step('安装器'):
                raise RuntimeError('private-output')
        self.assertEqual(progress.completed, 0)
        self.assertIn('未完成', out.getvalue())
        self.assertNotIn('private-output', out.getvalue())

    def test_tty_animation_thread_stops_on_cancellation(self):
        class TTY(io.StringIO):
            def isatty(self): return True
        threads = []
        real_thread = threading.Thread
        def create(*args, **kwargs):
            worker = real_thread(*args, **kwargs)
            threads.append(worker)
            return worker
        with patch.dict(os.environ, {'TERM': 'xterm'}), patch('progress.threading.Thread', side_effect=create):
            progress = Progress('安装', 1, stream=TTY())
            with self.assertRaises(KeyboardInterrupt):
                with progress.step('安装器'):
                    raise KeyboardInterrupt()
        self.assertEqual(len(threads), 1)
        self.assertFalse(threads[0].is_alive())


if __name__ == '__main__': unittest.main()
