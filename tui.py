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


def elide(value, cells):
    """Mark shortened status values so a clipped hostname cannot look complete."""
    if cell_width(value) <= cells:
        return clean_text(value)
    return fit(value, max(0, cells - 1)) + ('…' if cells > 0 else '')


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
        self.accent = curses.A_BOLD
        self.border = curses.A_DIM
        self.good = self.warning = self.danger = curses.A_BOLD
        self.selected = curses.A_REVERSE | curses.A_BOLD
        with contextlib.suppress(curses.error):
            curses.curs_set(0)
        if curses.has_colors():
            with contextlib.suppress(curses.error):
                curses.start_color()
                curses.use_default_colors()
                for pair, foreground, background in (
                    (1, curses.COLOR_CYAN, -1), (2, curses.COLOR_GREEN, -1),
                    (3, curses.COLOR_YELLOW, -1), (4, curses.COLOR_RED, -1),
                    (5, curses.COLOR_BLUE, -1), (6, curses.COLOR_BLACK, curses.COLOR_CYAN),
                ):
                    curses.init_pair(pair, foreground, background)
                self.accent = curses.color_pair(1) | curses.A_BOLD
                self.good = curses.color_pair(2) | curses.A_BOLD
                self.warning = curses.color_pair(3) | curses.A_BOLD
                self.danger = curses.color_pair(4) | curses.A_BOLD
                self.border = curses.color_pair(5)
                self.selected = curses.color_pair(6) | curses.A_BOLD
        self.dim = curses.A_DIM

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

    def tone(self, name):
        return {'good': self.good, 'warning': self.warning, 'danger': self.danger,
                'accent': self.accent, 'muted': self.dim}.get(name, 0)

    def box(self, y, x, height, width, title='', style=None, fill=None):
        """Draw a complete bordered region, clipping titles in terminal columns."""
        if width < 4 or height < 3:
            return
        style = self.border if style is None else style
        self.text(y, x, '╭' + '─' * (width - 2) + '╮', style)
        for row in range(y + 1, y + height - 1):
            self.text(row, x, '│', style)
            if fill is not None:
                self.text(row, x + 1, ' ' * (width - 2), fill)
            self.text(row, x + width - 1, '│', style)
        self.text(y + height - 1, x, '╰' + '─' * (width - 2) + '╯', style)
        if title:
            self.text(y, x + 2, fit(' ' + title + ' ', width - 5), style)

    def header(self, subtitle=''):
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        if height < 20 or width < 52:
            self.text(0, 0, '终端太小，请扩大至 52 列 × 20 行。', self.warning)
            self.text(2, 0, '按 Esc / Q 返回。')
            self.screen.refresh()
            return False
        self.text(1, 3, 'PrivateXUI', self.accent)
        self.text(1, max(20, width - 20), 'Clash / Mihomo', self.dim)
        self.text(2, 3, subtitle or '自有域名 · 私有订阅', self.dim)
        return True

    def frame(self, title, subtitle='', summary=()):
        if not self.header(subtitle):
            return None
        height, width = self.screen.getmaxyx()
        self.box(3, 2, height - 7, width - 4, title, self.accent)
        row = 5
        for line in summary:
            self.text(row, 4, fit(line, width - 9))
            row += 1
        return row + 1 if summary else row

    def footer(self, message):
        height, _ = self.screen.getmaxyx()
        self.text(height - 1, 3, message, self.dim)
        self.screen.refresh()

    def choose(self, title, items, *, summary=(), subtitle='', back=True, initial=None):
        """A bordered action list with clear focus, shared by all subpages."""
        selected = next((i for i, item in enumerate(items) if item[0] == initial), 0)
        while True:
            start = self.frame(title, subtitle, summary)
            height, width = self.screen.getmaxyx()
            if start is not None:
                room = max(1, height - start - 5)
                spacing = 2 if room >= len(items) * 2 - 1 else 1
                capacity = max(1, (room + spacing - 1) // spacing)
                first = max(0, selected - capacity + 1)
                for index in range(first, min(len(items), first + capacity)):
                    line = (' › ' if index == selected else '   ') + '%s  %s' % (index + 1, items[index][1])
                    if index == selected:
                        line = fit(line, width - 8)
                        line += ' ' * max(0, width - 8 - cell_width(line))
                    self.text(start + (index - first) * spacing, 4, line,
                              self.selected if index == selected else 0)
                self.text(height - 3, 3, items[selected][2], self.dim)
                self.footer('↑ ↓ 选择   Enter 确认   1–9 快捷键   Esc / Q ' + ('返回' if back else '退出'))
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

    def status_card(self, y, x, height, width, card):
        self.box(y, x, height, width, card['title'])
        badge, tone = card['badge']
        self.text(y + 1, x + 2, elide('● ' + badge, width - 4), self.tone(tone))
        for offset, (label, value, tone) in enumerate(card['rows'][:height - 3], 2):
            self.text(y + offset, x + 2, fit(label + '  ', width - 4), self.dim)
            start = x + 2 + cell_width(label + '  ')
            self.text(y + offset, start, elide(value, max(0, x + width - 2 - start)), self.tone(tone))

    def home(self, model, items):
        """Dashboard: two status cards, contextual next step, four action cards."""
        focus = model.get('focus')
        selected = next((i for i, item in enumerate(items) if item[0] == focus), 0)
        captions = {'install': '首次安装', 'subscription': '复制订阅',
                    'maintenance': '更新 / 检查', 'exit': '保留服务'}
        while True:
            ready = self.header('控制台  /  部署状态来自本机记录')
            height, width = self.screen.getmaxyx()
            if ready:
                roomy = width >= 78 and height >= 24
                top, card_height = (4, 7) if roomy else (3, 6)
                available = width - 6
                left_width = (available - 2) // 2
                right_width = available - 2 - left_width
                self.status_card(top, 3, card_height, left_width, model['server'])
                self.status_card(top, 5 + left_width, card_height, right_width, model['deployment'])
                notice = model['notice']
                notice_top = top + card_height + 1
                self.box(notice_top, 3, 4, available, notice['title'], self.tone(notice['tone']))
                notice_lines = notice.get('compact_lines', notice['lines']) if width < 78 else notice['lines']
                for offset, line in enumerate(notice_lines[:2], 1):
                    self.text(notice_top + offset, 5, elide(line, available - 4),
                              self.tone(notice['tone']) if offset == 2 else 0)
                action_height = 4 if roomy else 3
                action_top = height - action_height - 3
                gap = 1
                action_width = (available - gap * (len(items) - 1)) // len(items)
                for index, (value, label, _) in enumerate(items):
                    x = 3 + index * (action_width + gap)
                    length = available - (x - 3) if index == len(items) - 1 else action_width
                    active = index == selected
                    self.box(action_top, x, action_height, length,
                             style=self.accent if active else self.border,
                             fill=self.selected if active else None)
                    label_text = ('› ' if active else '  ') + '%s %s' % (index + 1, label)
                    self.text(action_top + 1, x + 1, fit(label_text, length - 2), self.selected if active else 0)
                    if roomy:
                        self.text(action_top + 2, x + 2, fit(captions.get(value, ''), length - 4),
                                  self.selected if active else self.dim)
                self.text(height - 2, 3, fit(items[selected][2], width - 7), self.dim)
                if model.get('shortcut'):
                    hint = '← → 选择  Enter 确认  A 优选设置  Q 退出'
                    if width >= 78:
                        hint = '← → / ↑ ↓ 选择   Enter 确认   1–4 快捷键   A 优选设置   Q 退出'
                else:
                    hint = '← → / ↑ ↓ 选择   Enter 确认   1–4 快捷键   Q 退出'
                self.footer(hint)
            key = self.key()
            if key in ('q', 'Q', '\x1b', '\x03', '0'):
                return None
            if not ready:
                continue
            if key in ('a', 'A') and model.get('shortcut'):
                return model['shortcut']
            if key in (curses.KEY_LEFT, curses.KEY_UP, 'h', 'k', '\x10'):
                selected = (selected - 1) % len(items)
            elif key in (curses.KEY_RIGHT, curses.KEY_DOWN, 'l', 'j', '\t', '\x0e'):
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
                    self.text(start, 4, label, self.accent)
                    before = tail(value[:cursor], max(1, width - 14))
                    shown = before + value[cursor:]
                    self.box(start + 2, 4, 3, width - 8, style=self.accent, fill=self.selected)
                    self.text(start + 3, 6, fit(shown, width - 13), self.selected)
                    self.text(start + 6, 4, fit(hint, width - 9), self.dim)
                    self.text(start + 8, 4, fit(error, width - 9), self.danger)
                    self.footer('Enter 下一步   ← → 移动   Ctrl+U 清空   Esc 返回')
                    with contextlib.suppress(curses.error):
                        self.screen.move(start + 3, min(width - 7, 6 + cell_width(before)))
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
