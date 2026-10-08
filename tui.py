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


def wrapped_lines(lines, width):
    """Wrap full summaries in display columns so they can be read by scrolling."""
    result = []
    for value in lines:
        for paragraph in str(value).split('\n'):
            current = ''
            for char in clean_text(paragraph):
                if current and cell_width(current + char) > width:
                    result.append(current)
                    current = ''
                current += char
            result.append(current)
    return result


def available():
    return (curses is not None and sys.stdin.isatty() and sys.stdout.isatty()
            and os.environ.get('TERM', '').lower() not in ('', 'dumb', 'unknown'))


class TerminalUI:
    def __init__(self, screen):
        self.screen = screen
        self.screen.keypad(True)
        self.screen.timeout(-1)
        self.normal = self.field = self.chart = 0
        self.strong = self.accent = curses.A_BOLD
        self.dim = self.border = self.chart2 = curses.A_DIM
        self.good = self.warning = self.danger = curses.A_BOLD
        self.selected = curses.A_REVERSE | curses.A_BOLD
        self.primary = self.selected
        with contextlib.suppress(curses.error):
            curses.curs_set(0)
        if 'NO_COLOR' not in os.environ and curses.has_colors():
            with contextlib.suppress(curses.error):
                curses.start_color()
                # Standard palette entries only: never rewrite the user's colors.
                palette = [('normal', 254, 232), ('strong', 255, 232), ('dim', 247, 232),
                           ('border', 236, 232), ('accent', 75, 232), ('selected', 111, 235),
                           ('primary', 255, 25), ('good', 108, 232), ('warning', 180, 232),
                           ('danger', 181, 232), ('field', 255, 234), ('chart', 252, 232),
                           ('chart2', 243, 232)]
                for pair, (name, foreground, background) in enumerate(palette, 1):
                    if getattr(curses, 'COLORS', 0) < 256:
                        foreground = {'accent': curses.COLOR_BLUE, 'good': curses.COLOR_GREEN,
                                      'warning': curses.COLOR_YELLOW, 'danger': curses.COLOR_RED}.get(name, curses.COLOR_WHITE)
                        background = curses.COLOR_BLUE if name in ('selected', 'primary') else curses.COLOR_BLACK
                    curses.init_pair(pair, foreground, background)
                    setattr(self, name, curses.color_pair(pair) | (curses.A_BOLD if name in ('strong', 'selected', 'primary') else 0))
                if hasattr(self.screen, 'bkgd'):
                    self.screen.bkgd(' ', self.normal)

    def text(self, y, x, value, style=0):
        height, width = self.screen.getmaxyx()
        if 0 <= y < height and 0 <= x < width - 1:
            with contextlib.suppress(curses.error):
                self.screen.addstr(y, x, fit(value, width - x - 1), style or self.normal)

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
        self.text(y, x, '┌' + '─' * (width - 2) + '┐', style)
        for row in range(y + 1, y + height - 1):
            self.text(row, x, '│', style)
            if fill is not None:
                self.text(row, x + 1, ' ' * (width - 2), fill)
            self.text(row, x + width - 1, '│', style)
        self.text(y + height - 1, x, '└' + '─' * (width - 2) + '┘', style)
        if title:
            self.text(y, x + 2, fit(' ' + title + ' ', width - 5), self.dim)

    def button(self, y, x, label, selected=False, width=None, primary=False):
        width = width or cell_width(label) + 4
        line = elide(('› ' if selected else '  ') + label, width)
        line += ' ' * max(0, width - cell_width(line))
        self.text(y, x, line, (self.primary if primary else self.selected) if selected else self.dim)

    def header(self, subtitle=''):
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        if height < 20 or width < 52:
            self.text(0, 0, '终端太小，请扩大至 52 列 × 20 行。', self.warning)
            self.text(2, 0, '按 Esc / Q 返回。')
            self.screen.refresh()
            return False
        self.text(1, 2, 'Private XUI', self.strong)
        caption = 'Clash / Mihomo' if width >= 80 else 'SSH 控制台'
        self.text(1, width - cell_width(caption) - 2, caption, self.dim)
        self.text(2, 2, elide('/ ' + (subtitle or '自有域名 · 私有订阅'), width - 4), self.dim)
        self.text(3, 2, '─' * (width - 4), self.border)
        return True

    def frame(self, title, subtitle='', summary=()):
        if not self.header(title + (' / ' + subtitle if subtitle else '')):
            return None
        height, width = self.screen.getmaxyx()
        row = 5
        lines = wrapped_lines(summary, width - 4)
        room = max(1, height - 14)
        for line in lines[:room]:
            self.text(row, 2, line, self.dim)
            row += 1
        if len(lines) > room:
            self.text(row - 1, 2, '…  ? 查看完整摘要', self.accent)
        return row + 1 if summary else row

    def footer(self, message):
        height, width = self.screen.getmaxyx()
        self.text(height - 3, 2, '─' * (width - 4), self.border)
        self.text(height - 2, 2, fit(message, width - 4), self.dim)
        self.screen.refresh()

    def details(self, title, lines):
        """Read a complete, scrollable explanation without changing its caller."""
        offset = 0
        while True:
            ready = self.header(title)
            height, width = self.screen.getmaxyx()
            if ready:
                rows = wrapped_lines(lines, width - 4)
                room = max(1, height - 10)
                offset = min(offset, max(0, len(rows) - room))
                for index, line in enumerate(rows[offset:offset + room], 5):
                    self.text(index, 2, line)
                if len(rows) > room:
                    self.text(height - 4, 2, '%s–%s / %s 行' % (offset + 1, min(offset + room, len(rows)), len(rows)), self.dim)
                self.footer('↑↓ 阅读   Enter / Esc / ? 返回')
            key = self.key()
            if key in ('\n', '\r', curses.KEY_ENTER, '\x1b', '\x03', 'q', 'Q', '?'):
                return
            if not ready:
                continue
            if key in (curses.KEY_DOWN, 'j'):
                offset += 1
            elif key in (curses.KEY_UP, 'k'):
                offset = max(0, offset - 1)

    def choose(self, title, items, *, summary=(), subtitle='', back=True, initial=None):
        """Compact action rows with a subtle, keyboard-visible focus."""
        if not items:
            return None
        selected = next((i for i, item in enumerate(items) if item[0] == initial), 0)
        while True:
            start = self.frame(title, subtitle, summary)
            height, width = self.screen.getmaxyx()
            if start is not None:
                room = max(1, height - start - 5)
                spacing = 3 if room >= len(items) * 3 - 1 else 2 if room >= len(items) * 2 - 1 else 1
                capacity = max(1, (room + spacing - 1) // spacing)
                first = max(0, selected - capacity + 1)
                for index in range(first, min(len(items), first + capacity)):
                    row = start + (index - first) * spacing
                    self.button(row, 2, '%s  %s' % (index + 1, items[index][1]), index == selected, width - 4)
                    if spacing >= 2:
                        self.text(row + 1, 4, elide(items[index][2], width - 8), self.dim)
                if spacing == 1:
                    self.text(height - 4, 2, elide(items[selected][2], width - 4), self.dim)
                self.footer('↑↓ 选择  Enter 确认  1–9 快捷  Esc ' + ('返回' if back else '退出') + '  ? 帮助')
            key = self.key()
            if key in ('q', 'Q', '\x1b', '\x03', '0'):
                return None
            if start is None:
                continue
            if key == '?':
                self.details(title, list(summary) + [items[selected][1], items[selected][2], '',
                             '↑↓ 选择操作，Enter 确认，1–9 可直接选择。', 'Esc / Q 返回；不会执行未选择的操作。'])
            elif key in (curses.KEY_UP, 'k', '\x10'):
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
        self.text(y + 1, x + 2, '●', self.tone(tone))
        self.text(y + 1, x + 4, elide(badge, width - 6), self.strong)
        for offset, (label, value, tone) in enumerate(card['rows'][:height - 4], 3):
            self.text(y + offset, x + 2, fit(label + '  ', width - 4), self.dim)
            start = x + 2 + cell_width(label + '  ')
            self.text(y + offset, start, elide(value, max(0, x + width - 2 - start)), self.tone(tone))

    def home(self, model, items):
        """Deployment summary and the existing four actions, fitted to the terminal."""
        focus = model.get('focus')
        selected = next((i for i, item in enumerate(items) if item[0] == focus), 0)
        captions = {'install': '首次安装', 'subscription': '查看完整地址',
                    'maintenance': '更新 / 检查', 'exit': '保留服务'}
        while True:
            ready = self.header('控制台  /  部署状态来自本机记录')
            height, width = self.screen.getmaxyx()
            if ready:
                notice = model['notice']
                if width >= 80 and height >= 24:
                    spacious = height >= 34
                    top, card_height = (7, 8) if spacious else (5, 7)
                    card_width = (width - 6) // 2
                    self.status_card(top, 2, card_height, card_width, model['server'])
                    self.status_card(top, card_width + 4, card_height, width - card_width - 6, model['deployment'])
                    notice_top = top + card_height + (2 if spacious else 1)
                    self.box(notice_top, 2, 4 if spacious else 3, width - 4)
                    self.text(notice_top + 1, 4, elide(notice['title'], width - 8), self.tone(notice['tone']))
                    if spacious and notice['lines']:
                        self.text(notice_top + 2, 4, elide(notice['lines'][0], width - 8), self.dim)
                    action_top = height - 10 if spacious else height - 6
                    action_width = (width - 4 - (len(items) - 1)) // len(items)
                    for index, (value, label, _) in enumerate(items):
                        x = 2 + index * (action_width + 1)
                        self.button(action_top, x, '%s %s' % (index + 1, label), index == selected, action_width)
                        if spacious:
                            self.text(action_top + 2, x + 2, elide(captions.get(value, ''), action_width - 2), self.dim)
                    if spacious:
                        self.text(5, 2, '服务与配置', self.strong)
                else:
                    rows = [('3x-ui', *model['server']['badge']), ('部署', *model['deployment']['badge'])] + list(model['deployment']['rows'][:3])
                    for index, (label, value, tone) in enumerate(rows):
                        self.text(5 + index, 2, label, self.dim)
                        self.text(5 + index, 10, elide(value, width - 12), self.tone(tone))
                    self.text(11, 2, elide(notice['title'], width - 4), self.tone(notice['tone']))
                    compact = notice.get('compact_lines', notice['lines'])
                    if compact:
                        self.text(12, 2, elide(compact[0], width - 4), self.dim)
                    action_width = (width - 6) // 2
                    for index, (_, label, _) in enumerate(items):
                        self.button(height - 6 + index // 2, 2 + index % 2 * (action_width + 2), '%s %s' % (index + 1, label), index == selected, action_width)
                self.text(height - 4, 2, elide(items[selected][2], width - 4), self.dim)
                hint = '1–4 操作  D 流量  ' + ('A 优选  ' if model.get('shortcut') else '') + 'Q 退出  ? 帮助'
                if width >= 80:
                    hint = '←→ / ↑↓ 选择  Enter 打开  ' + hint
                self.footer(hint)
            key = self.key()
            if key in ('q', 'Q', '\x1b', '\x03', '0'):
                return None
            if not ready:
                continue
            if key == '?':
                lines = ['首页：1 部署，2 订阅，3 维护，4 退出。', 'D 查看流量；A 在有提示时进入自动优选。']
                for card in (model['server'], model['deployment']):
                    lines += ['', card['title'] + ' · ' + card['badge'][0]] + [label + '  ' + str(value) for label, value, _ in card['rows']]
                self.details('控制台 / 状态与帮助', lines + ['', notice['title']] + list(notice['lines']))
            elif key in ('d', 'D'):
                return 'dashboard'
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

    def dashboard(self, provider):
        """Read-only, live terminal dashboard. No browser, sockets or credentials."""
        import time
        page, day_index, offset, query = 0, 0, 0, ''
        periods = [1, 7, 30]
        labels = ['24 小时', '7 天', '30 天']
        data, error, next_refresh, paused = None, '', 0, False
        self.screen.timeout(1000)

        def size(value):
            value = max(0, float(value or 0))
            units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
            index = 0
            while value >= 1024 and index < 4:
                value /= 1024
                index += 1
            return ('%.1f ' if index else '%.0f ') % value + units[index]

        def stamp(value):
            return time.strftime('%m-%d %H:%M', time.localtime(int(value)))

        def spark(field, columns):
            points = data.get('points', [])
            if not points:
                return '等待采样数据', 0
            bins = [None] * max(1, columns)
            start = data['now'] - periods[day_index] * 86400
            for point in points:
                index = min(len(bins) - 1, max(0, int((point['ts'] - start) / (periods[day_index] * 86400) * len(bins))))
                bins[index] = (bins[index] or 0) + point[field]
            peak = max([v for v in bins if v is not None] or [0])
            blocks = '▁▂▃▄▅▆▇█'
            return ''.join('·' if v is None else blocks[min(7, int(v / peak * 7))] if peak else blocks[0] for v in bins), peak

        try:
            while True:
                if time.monotonic() >= next_refresh and not paused:
                    try:
                        data = provider(periods[day_index], query)
                        error = ''
                    except Exception as exc:
                        error = str(exc)
                    next_refresh = time.monotonic() + 5
                ready = self.header('Dashboard / 仅本项目 · ' + ('已暂停刷新' if paused else '30 秒采样 · 5 秒刷新'))
                height, width = self.screen.getmaxyx()
                if ready:
                    tab_y = 4 if height <= 20 else 5
                    x = 2
                    for index, title in enumerate(['1 概览', '2 热门域名', '3 连接历史']):
                        self.button(tab_y, x, title, index == page)
                        x += cell_width(title) + 6
                    period_y = tab_y + 1 if height < 26 else 7
                    x = 2
                    for index, label in enumerate(labels):
                        self.text(period_y, x, ('[' + label + ']') if index == day_index else ' ' + label + ' ',
                                  self.accent if index == day_index else self.dim)
                        x += cell_width(label) + 4
                    status_y = period_y + 1 if height < 26 else 9
                    content_y = status_y + 2
                    if error:
                        self.text(content_y, 2, '统计暂不可用', self.danger)
                        for row, line in enumerate(wrapped_lines([error], width - 4)[:max(1, height - content_y - 8)], content_y + 2):
                            self.text(row, 2, line, self.warning)
                        self.text(height - 5, 2, elide('R 重试；未启用时先在维护中启用采集。', width - 4), self.dim)
                    elif data is not None:
                        status = data.get('status', {})
                        sampled = int(data.get('meta', {}).get('sample_at') or 0)
                        stale = not sampled or data['now'] - sampled > 90
                        notices = [v for k, v in status.items() if k in ('traffic', 'history') and v != '正常']
                        if stale:
                            notices.append('流量采样已过期或尚未开始')
                        message = ' / '.join(notices) if notices else '采集正常 · 仅覆盖启用后的已采集时段'
                        if data.get('demo'):
                            message = '演示数据 · ' + message
                        self.text(status_y, 2, elide(message, width - 4), self.warning if notices else self.dim)
                        if page == 0:
                            traffic_ready = bool(sampled)
                            history_ready = status.get('history') == '正常' or data['totals']['connections'] > 0
                            values = [size(data['totals']['down']) if traffic_ready else '—',
                                      size(data['totals']['up']) if traffic_ready else '—',
                                      str(data['totals']['connections']) if history_ready else '—',
                                      size(data['rates']['down']) + '/s' if traffic_ready else '—']
                            metric_labels = ['下载 / 所选时段', '上传 / 所选时段', '连接记录', '近两分钟下载均速']
                            if width >= 100 and height >= 34:
                                card_width = (width - 7) // 4
                                for index, (label, value) in enumerate(zip(metric_labels, values)):
                                    x = 2 + index * (card_width + 1)
                                    self.box(content_y, x, 5, card_width, label)
                                    self.text(content_y + 2, x + 2, elide(value, card_width - 4), self.strong)
                                chart_y, chart_width = content_y + 7, (width - 7) * 2 // 3
                                self.box(chart_y, 2, 10, chart_width, '历史趋势')
                                for row, field, label, style in [(chart_y + 2, 'down', '下载', self.chart), (chart_y + 5, 'up', '上传', self.chart2)]:
                                    trend, peak = spark(field, chart_width - 4)
                                    self.text(row, 4, label + ('  · 当前图列峰值 ' + size(peak) if data.get('points') else ''), self.dim)
                                    self.text(row + 1, 4, trend, style)
                                self.text(chart_y + 8, 4, stamp(data['now'] - periods[day_index] * 86400), self.dim)
                                self.text(chart_y + 8, chart_width - 13, stamp(data['now']), self.dim)
                                x, card_width = chart_width + 4, width - chart_width - 6
                                self.box(chart_y, x, 10, card_width, '节点当前累计')
                                counters = data.get('counters', [])
                                if not counters:
                                    self.text(chart_y + 2, x + 2, '尚无节点计数', self.dim)
                                for index, item in enumerate(counters[:3]):
                                    self.text(chart_y + 2 + index * 2, x + 2, item['protocol'].upper(), self.dim)
                                    self.text(chart_y + 3 + index * 2, x + 2,
                                              elide('↑ %s / ↓ %s' % (size(item['up']), size(item['down'])), card_width - 4))
                                self.text(height - 5, 2, '上/下分别缩放 · 点号为无采样 · 当前累计可由面板重置', self.dim)
                            else:
                                for index in range(2):
                                    x = 2 + index * ((width - 4) // 2)
                                    self.text(content_y, x, metric_labels[index], self.dim)
                                    self.text(content_y + 1, x, values[index], self.strong)
                                self.text(content_y + (2 if width < 80 else 3), 2, '连接记录 ' + values[2], self.dim)
                                self.text(content_y + 3, 2 if width < 80 else width // 2,
                                          elide('近两分钟下载均速 ' + values[3], width - 4 if width < 80 else width // 2 - 2), self.dim)
                                chart_y = content_y + (4 if height <= 20 else 5)
                                self.text(chart_y, 2, '历史趋势 · 独立缩放 / · 无采样', self.dim)
                                for row, field, label, style in [(chart_y + 1, 'down', '下', self.chart), (chart_y + 2, 'up', '上', self.chart2)]:
                                    trend, _ = spark(field, width - 7)
                                    self.text(row, 2, label + ' ' + trend, style)
                        elif page == 1:
                            items = data.get('top', [])
                            room = max(1, height - content_y - 7)
                            offset = min(offset, max(0, len(items) - room))
                            self.text(content_y, 2, '#', self.dim)
                            self.text(content_y, 7, '目标域名', self.dim)
                            self.text(content_y, width - 17, '连接次数', self.dim)
                            self.text(content_y + 1, 2, '─' * (width - 4), self.border)
                            if not items:
                                self.text(content_y + 3, 2, '尚无可识别的域名记录。', self.dim)
                            largest = max([item['connections'] for item in items] or [1])
                            for row, (index, item) in enumerate(enumerate(items[offset:offset + room], offset), content_y + 2):
                                self.text(row, 2, '%02d' % (index + 1), self.dim)
                                self.text(row, 7, elide(item['target'], width - 26))
                                self.text(row, width - 17, str(item['connections']).rjust(7), self.strong)
                                if width >= 90:
                                    self.text(row, width - 8, '━' * max(1, round(item['connections'] / largest * 5)), self.chart2)
                            self.text(height - 5, 2, '按连接记录计数，非浏览量；含接受与拒绝。', self.dim)
                            self.text(height - 4, 2, elide('仅 IP %s 条 · %s 次采样中断 · ↑↓ 滚动' % (data['ip_only'], data.get('meta', {}).get('gaps', 0)), width - 4), self.dim)
                        else:
                            items = data.get('history', [])
                            self.text(content_y, 2, elide('搜索 / ' + (query or '全部目标'), width - 4), self.dim)
                            table_y = content_y + 2
                            room = max(1, height - table_y - 7)
                            offset = min(offset, max(0, len(items) - room))
                            target_width = width - 32
                            self.text(table_y, 2, '时间', self.dim)
                            self.text(table_y, 15, '目标:端口', self.dim)
                            self.text(table_y, width - 15, '协议', self.dim)
                            self.text(table_y, width - 7, '结果', self.dim)
                            self.text(table_y + 1, 2, '─' * (width - 4), self.border)
                            for row, item in enumerate(items[offset:offset + room], table_y + 2):
                                target = ('[' + item['target'] + ']') if ':' in item['target'] else item['target']
                                self.text(row, 2, stamp(item['ts']), self.dim)
                                self.text(row, 15, elide(target + ':' + str(item['port']), target_width))
                                self.text(row, width - 15, fit(item['protocol'].upper(), 6), self.dim)
                                self.text(row, width - 7, '接受' if item['outcome'] == 'accepted' else '拒绝',
                                          self.dim if item['outcome'] == 'accepted' else self.warning)
                            if not items:
                                self.text(table_y + 3, 2, '暂无符合条件的连接记录。', self.dim)
                            self.text(height - 5, 2, '最近 100 条匹配记录 · 服务器本地时间', self.dim)
                    hint = '1–3 页面  ←→ 时段  ↑↓ 滚动  / 搜索  R 刷新  P 暂停  C 累计  ? 帮助  Esc 返回'
                    if width < 100:
                        hint = '1–3 页 ←→ 时段 / 搜索 C 累计 ? 帮助'
                    self.footer(hint)
                key = self.key()
                if key in ('q', 'Q', '\x1b', '\x03', '0'):
                    return
                if not ready:
                    continue
                if key == '?':
                    explanations = ['1–3 切换页面；←→ 选择 24 小时、7 天、30 天。',
                                    '↑↓ 滚动记录；/ 搜索域名或 IP。', 'R 刷新；P 暂停/恢复显示刷新；C 查看当前累计。',
                                    'Esc / Q 返回，后台采集继续运行。', '',
                                    '均速：近两分钟的平均值，不是瞬时速度。',
                                    '趋势：上传/下载分别缩放；点号表示没有采样。',
                                    '热门域名：连接记录数，包含接受和拒绝，并非浏览量。']
                    if data:
                        explanations += ['', '采集状态'] + [str(v) for k, v in data.get('status', {}).items() if k in ('traffic', 'history')]
                    if error:
                        explanations += ['', error]
                    self.details('Dashboard / 帮助与统计口径', explanations)
                elif key in ('c', 'C'):
                    lines = ['3x-ui 当前累计计数，可由面板重置。', '与所选时段流量分开统计。', '']
                    lines += ['%s  ↑ %s  ↓ %s' % (item['protocol'].upper(), size(item['up']), size(item['down']))
                              for item in (data or {}).get('counters', [])]
                    if not (data or {}).get('counters'):
                        lines.append('尚无可读节点计数。')
                    self.details('Dashboard / 节点当前累计', lines)
                elif key in ('1', '2', '3'):
                    page, offset = int(key) - 1, 0
                elif key in (curses.KEY_LEFT, curses.KEY_RIGHT):
                    day_index = (day_index + (1 if key == curses.KEY_RIGHT else -1)) % 3
                    offset, next_refresh, paused = 0, 0, False
                elif key in (curses.KEY_DOWN, 'j'):
                    offset += 1
                elif key in (curses.KEY_UP, 'k'):
                    offset = max(0, offset - 1)
                elif key in ('r', 'R'):
                    next_refresh, paused = 0, False
                elif key in ('p', 'P'):
                    paused = not paused
                    if not paused:
                        next_refresh = 0
                elif key == '/':
                    self.screen.timeout(-1)
                    try:
                        query = self.ask('搜索连接历史', '目标域名或 IP；清空后回车显示全部', default=query, allow_empty=True) or ''
                        page, offset, next_refresh, paused = 2, 0, 0, False
                    except Cancelled:
                        pass
                    finally:
                        self.screen.timeout(1000)
        finally:
            self.screen.timeout(-1)

    def ask(self, title, label, validator=lambda value: value, *, default='', hint='', allow_empty=False, secret=False, preserve_whitespace=False):
        """Edit text locally; secret fields never draw their value or validator error."""
        value, cursor, error = str(default), len(str(default)), ''
        with contextlib.suppress(curses.error):
            curses.curs_set(1)
        try:
            while True:
                ready = self.header(title)
                height, width = self.screen.getmaxyx()
                if ready:
                    self.text(5, 2, '填写配置', self.strong)
                    self.text(7, 2, elide(label, width - 4), self.dim)
                    display = '*' * len(value) if secret else value
                    left = 0
                    while cell_width(display[left:cursor]) > width - 10:
                        left += 1
                    before = display[left:cursor]
                    self.box(9, 2, 3, width - 4, style=self.accent, fill=self.field)
                    self.text(10, 4, fit(display[left:], width - 8), self.field)
                    for row, line in enumerate(wrapped_lines([hint], width - 4)[:max(1, height - 18)], 13):
                        self.text(row, 2, line, self.dim)
                    if secret and not hint:
                        self.text(13, 2, '输入已隐藏；密码中的空格会原样保留。', self.dim)
                    self.text(height - 5, 2, elide(error, width - 4), self.danger)
                    self.footer('Enter 下一步  ←→ 移动  Ctrl+U 清空  Esc 返回')
                    with contextlib.suppress(curses.error):
                        self.screen.move(10, min(width - 5, 4 + cell_width(before)))
                    self.screen.refresh()
                key = self.key()
                if key in ('\x1b', '\x03'):
                    raise Cancelled()
                if not ready:
                    continue
                if key in ('\n', '\r', curses.KEY_ENTER):
                    raw = value if secret or preserve_whitespace else value.strip()
                    if not raw and allow_empty:
                        return None
                    if not raw:
                        error = '此项不能为空。'
                        continue
                    try:
                        return validator(raw)
                    except (ValueError, OSError, KeyError) as exc:
                        error = '输入无效，请检查后重试。' if secret else str(exc)
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
        """Keep every impact readable; destructive actions initially select cancel."""
        choices = [('no', '返回'), ('yes', '确认并继续')]
        if not destructive:
            choices.reverse()
        selected, offset = 0, 0
        while True:
            ready = self.header('确认操作')
            height, width = self.screen.getmaxyx()
            if ready:
                self.text(5, 2, elide(title, width - 4), self.danger if destructive else self.strong)
                lines = wrapped_lines(summary, width - 4)
                room = height - 14
                offset = min(offset, max(0, len(lines) - room))
                for row, line in enumerate(lines[offset:offset + room], 7):
                    self.text(row, 2, line)
                if len(lines) > room:
                    self.text(height - 7, 2, '↑↓ 阅读摘要  %s–%s / %s 行' % (offset + 1, min(offset + room, len(lines)), len(lines)), self.accent)
                x = 2
                for index, (action, label) in enumerate(choices):
                    button_width = 24 if action == 'yes' else 14
                    self.button(height - 5, x, label, index == selected, button_width, primary=action == 'yes')
                    x += button_width + 3
                self.footer('↑↓ 摘要  ←→ 选择  Enter 确认  Esc 返回')
            key = self.key()
            if key in ('\x1b', '\x03', 'q', 'Q', '0'):
                return False
            if not ready:
                continue
            if key in (curses.KEY_DOWN, 'j'):
                offset += 1
            elif key in (curses.KEY_UP, 'k'):
                offset = max(0, offset - 1)
            elif key in (curses.KEY_LEFT, curses.KEY_RIGHT, 'h', 'l', '\t'):
                selected = 1 - selected
            elif key in ('\n', '\r', curses.KEY_ENTER):
                return choices[selected][0] == 'yes'
            elif key in ('1', '2'):
                return choices[int(key) - 1][0] == 'yes'
            elif key == '?':
                self.details('确认操作 / 帮助', ['↑↓ 阅读完整摘要，←→ 选择按钮。', 'Enter 执行选中的操作；Esc / Q 取消。',
                             '数字 1 / 2 对应从左到右的两个按钮。'])

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
