"""Small, dependency-free terminal UI for an SSH session.

Curses stays on the main thread. Long tasks temporarily restore normal terminal
mode so progress output, getpass, and Ctrl+C keep their usual behavior.
"""
import contextlib
import locale
import os
import sys
import unicodedata

try:
    import curses
except ImportError:  # Minimal Python builds can still use the text menu.
    curses = None


class Cancelled(Exception):
    """The user returned from a screen without submitting it."""


def clean_text(value):
    return ''.join(c for c in str(value) if c == '\t' or not unicodedata.category(c).startswith('C')).replace('\t', ' ')


def cell_width(value):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1
               for c in clean_text(value))


def fit(value, cells):
    """Clip in terminal columns without splitting a CJK character."""
    result, width = [], 0
    for char in clean_text(value):
        size = cell_width(char)
        if width + size > max(0, cells):
            break
        result.append(char)
        width += size
    return ''.join(result)


def tail(value, cells):
    result, width = [], 0
    for char in reversed(clean_text(value)):
        size = cell_width(char)
        if width + size > max(0, cells):
            break
        result.append(char)
        width += size
    return ''.join(reversed(result))


def available():
    return (curses is not None and sys.stdin.isatty() and sys.stdout.isatty()
            and os.environ.get('TERM', '').lower() not in ('', 'dumb', 'unknown'))


class TerminalUI:
    def __init__(self, screen):
        self.screen = screen
        self.screen.keypad(True)
        self.screen.timeout(-1)
        self.accent = self.dim = self.selected = 0
        with contextlib.suppress(curses.error):
            curses.curs_set(0)
        if curses.has_colors():
            with contextlib.suppress(curses.error):
                curses.start_color()
                curses.use_default_colors()
                curses.init_pair(1, curses.COLOR_CYAN, -1)
                self.accent = curses.color_pair(1) | curses.A_BOLD
        self.dim = curses.A_DIM
        self.selected = curses.A_REVERSE | curses.A_BOLD

    def text(self, y, x, value, style=0):
        height, width = self.screen.getmaxyx()
        if 0 <= y < height and 0 <= x < width - 1:
            with contextlib.suppress(curses.error):
                self.screen.addstr(y, x, fit(value, width - x - 1), style)

    def key(self):
        try:
            return self.screen.get_wch()
        except KeyboardInterrupt:
            return '\x03'
        except curses.error:
            return None

    def frame(self, title, subtitle='', summary=()):
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        if height < 20 or width < 52:
            self.text(0, 0, '终端太小，请扩大至 52 列 × 20 行。', self.accent)
            self.text(2, 0, '按 Esc / Q 返回。')
            self.screen.refresh()
            return None
        self.text(1, 3, 'PRIVATE XUI', self.accent)
        self.text(2, 3, subtitle or '自有域名 · Clash / Mihomo')
        self.text(3, 3, '─' * (width - 7), self.dim)
        row = 5
        for line in summary:
            self.text(row, 3, line)
            row += 1
        if summary:
            row += 1
        self.text(row, 3, title, self.accent)
        return row + 2

    def footer(self, message):
        height, _ = self.screen.getmaxyx()
        self.text(height - 2, 3, message, self.dim)
        self.screen.refresh()

    def choose(self, title, items, *, summary=(), subtitle='', back=True):
        """items contain (stable value, label, short description)."""
        selected = 0
        while True:
            start = self.frame(title, subtitle, summary)
            height, width = self.screen.getmaxyx()
            if start is not None:
                capacity = max(1, height - start - 5)
                first = max(0, selected - capacity + 1)
                for index in range(first, min(len(items), first + capacity)):
                    prefix = '› ' if index == selected else '  '
                    line = prefix + '%s  %s' % (index + 1, items[index][1])
                    if index == selected:
                        line = fit(line, width - 7)
                        line += ' ' * max(0, width - 7 - cell_width(line))
                    self.text(start + index - first, 3, line, self.selected if index == selected else 0)
                self.text(height - 4, 3, items[selected][2], self.dim)
                self.footer('↑ ↓ 选择   Enter 确认   数字快捷键   Esc / Q ' + ('返回' if back else '退出'))
            key = self.key()
            if key in ('q', 'Q', '\x1b', '\x03', '0'):
                return None
            if start is None:
                continue
            if key in (curses.KEY_UP, 'k', '\x10'):
                selected = (selected - 1) % len(items)
            elif key in (curses.KEY_DOWN, 'j', '\t', '\x0e'):
                selected = (selected + 1) % len(items)
            elif key in ('\n', '\r', curses.KEY_ENTER):
                return items[selected][0]
            elif isinstance(key, str) and key in '123456789' and int(key) <= len(items):
                return items[int(key) - 1][0]

    def ask(self, title, label, validator=lambda value: value, *, default='', hint='', allow_empty=False):
        value, cursor, error = str(default), len(str(default)), ''
        with contextlib.suppress(curses.error):
            curses.curs_set(1)
        try:
            while True:
                start = self.frame(title, '填写配置 · Enter 下一步 · Esc 取消')
                height, width = self.screen.getmaxyx()
                if start is not None:
                    self.text(start, 3, label, self.accent)
                    before = tail(value[:cursor], max(1, width - 10))
                    shown = before + value[cursor:]
                    self.text(start + 2, 3, ' ' * (width - 7), curses.A_REVERSE)
                    self.text(start + 2, 4, fit(shown, width - 9), curses.A_REVERSE)
                    self.text(start + 4, 3, hint, self.dim)
                    self.text(start + 6, 3, error)
                    self.footer('Enter 下一步   ← → 移动   Ctrl+U 清空   Esc 返回')
                    with contextlib.suppress(curses.error):
                        self.screen.move(start + 2, min(width - 3, 4 + cell_width(before)))
                    self.screen.refresh()
                key = self.key()
                if key in ('\x1b', '\x03'):
                    raise Cancelled()
                if start is None:
                    continue
                if key in ('\n', '\r', curses.KEY_ENTER):
                    raw = value.strip()
                    if not raw and allow_empty:
                        return None
                    if not raw:
                        error = '此项不能为空。'
                        continue
                    try:
                        return validator(raw)
                    except (ValueError, OSError, KeyError) as exc:
                        error = str(exc)
                elif key in (curses.KEY_BACKSPACE, '\b', '\x7f'):
                    if cursor:
                        value = value[:cursor - 1] + value[cursor:]
                        cursor -= 1
                elif key == curses.KEY_DC:
                    value = value[:cursor] + value[cursor + 1:]
                elif key == curses.KEY_LEFT:
                    cursor = max(0, cursor - 1)
                elif key == curses.KEY_RIGHT:
                    cursor = min(len(value), cursor + 1)
                elif key in (curses.KEY_HOME, '\x01'):
                    cursor = 0
                elif key in (curses.KEY_END, '\x05'):
                    cursor = len(value)
                elif key == '\x15':
                    value, cursor = '', 0
                elif isinstance(key, str) and key.isprintable() and len(value) < 1024:
                    value = value[:cursor] + key + value[cursor:]
                    cursor += 1
        finally:
            with contextlib.suppress(curses.error):
                curses.curs_set(0)

    def confirm(self, title, summary, *, destructive=False):
        choices = [('no', '返回', '保留当前配置'), ('yes', '确认并继续', '执行上方列出的操作')]
        if not destructive:
            choices.reverse()
        return self.choose(title, choices, summary=summary) == 'yes'

    def console(self, operation):
        """Run terminal I/O in normal mode, then restore the TUI reliably."""
        curses.def_prog_mode()
        curses.endwin()
        try:
            # This is only reachable on an interactive terminal.
            sys.stdout.write('\033[2J\033[H')
            sys.stdout.flush()
            operation()
            try:
                input('\n按 Enter 返回…')
            except (KeyboardInterrupt, EOFError):
                pass
        finally:
            curses.reset_prog_mode()
            with contextlib.suppress(curses.error):
                curses.curs_set(0)
            self.screen.keypad(True)
            self.screen.clear()
            self.screen.refresh()


def run(controller):
    locale.setlocale(locale.LC_ALL, '')
    curses.wrapper(lambda screen: controller(TerminalUI(screen)))
