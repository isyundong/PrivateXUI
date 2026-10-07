#!/usr/bin/env python3
"""Independent, in-memory TUI design preview. Never imports the production app.

Run: python3 design/tui-shadcn/preview.py
Export: python3 design/tui-shadcn/preview.py --export-dir /tmp/private-xui-design
Public API: Session(screen=...), render(session, width, height) -> Canvas.
Canvas.grid is a row-major list of Cell(char, style); wide-character continuations
have char="". Canvas.to_html() exports the exact same drawing used by curses.
"""
import argparse
import curses
import datetime as dt
import html
import math
import os
from pathlib import Path
from dataclasses import dataclass, field
import unicodedata


SCREENS = ('home', 'dashboard', 'domains', 'history', 'maintenance', 'subscription', 'form', 'confirm')
STATES = ('normal', 'warning', 'error', 'empty', 'stale', 'no-log', 'gap')
STATE_NAMES = dict(zip(STATES, ('正常', '待处理', '异常', '未开始', '采样过期', '缺少日志', '采样中断')))
TITLES = dict(zip(SCREENS, ('控制台', '流量分析', '热门域名', '连接历史', '维护', '订阅', '部署', '确认操作')))
TITLES.update(help='键盘帮助', search='搜索连接历史', settings='订阅设置', detail='检查结果', collector='采集管理')
# foreground, background, bold. Exact preset: nova / neutral / blue / radius none.
PALETTE = {
    'text': ('#e5e5e5', '#0a0a0a', False), 'strong': ('#fafafa', '#0a0a0a', True),
    'muted': ('#a3a3a3', '#0a0a0a', False), 'dim': ('#737373', '#0a0a0a', False),
    'border': ('#333333', '#0a0a0a', False), 'blue': ('#60a5fa', '#0a0a0a', False),
    'focus': ('#93c5fd', '#262626', True), 'primary': ('#ffffff', '#1e40af', True),
    'good': ('#86b99a', '#0a0a0a', False), 'warning': ('#d4b678', '#0a0a0a', False),
    'danger': ('#e59b9b', '#0a0a0a', False), 'chart': ('#d4d4d4', '#0a0a0a', False),
    'chart2': ('#737373', '#0a0a0a', False), 'field': ('#f5f5f5', '#171717', False),
    'cursor': ('#0a0a0a', '#e5e5e5', False),
}


def cells(value):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in str(value))


def clipped(value, width, ellipsis=True):
    value = str(value)
    if cells(value) <= width:
        return value
    end, used, budget = '', 0, max(0, width - (1 if ellipsis else 0))
    for char in value:
        size = cells(char)
        if used + size > budget:
            break
        end, used = end + char, used + size
    return end + ('…' if ellipsis and width else '')


def wrapped(value, width):
    lines, current = [], ''
    for paragraph in str(value).split('\n'):
        for char in paragraph:
            if cells(current + char) > width:
                lines.append(current)
                current = ''
            current += char
        lines.append(current)
        current = ''
    return lines


@dataclass
class Cell:
    char: str = ' '
    style: str = 'text'


class Canvas:
    def __init__(self, width, height):
        self.width, self.height = width, height
        self.cursor = None
        self.grid = [[Cell() for _ in range(width)] for _ in range(height)]

    def text(self, y, x, value, style='text', width=None):
        if y < 0 or y >= self.height or x >= self.width:
            return
        width = self.width - x if width is None else min(width, self.width - x)
        for char in clipped(value, max(0, width)):
            size = cells(char)
            if size == 0:
                if x > 0:
                    self.grid[y][x - 1].char += char
                continue
            if x >= 0 and x + size <= self.width:
                self.grid[y][x] = Cell(char, style)
                for offset in range(1, size):
                    self.grid[y][x + offset] = Cell('', style)
            x += size

    def fill(self, y, x, width, height=1, style='text'):
        for row in range(max(0, y), min(self.height, y + height)):
            for col in range(max(0, x), min(self.width, x + width)):
                self.grid[row][col] = Cell(' ', style)

    def line(self, y, x, width, style='border'):
        self.text(y, x, '─' * max(0, width), style)

    def box(self, y, x, width, height, title='', style='border'):
        self.text(y, x, '┌' + '─' * (width - 2) + '┐', style)
        self.text(y + height - 1, x, '└' + '─' * (width - 2) + '┘', style)
        for row in range(y + 1, y + height - 1):
            self.text(row, x, '│', style)
            self.text(row, x + width - 1, '│', style)
        if title:
            self.text(y, x + 2, ' ' + clipped(title, width - 6) + ' ', 'muted')

    def paragraph(self, y, x, value, width, style='muted', limit=None):
        lines = wrapped(value, width)
        for offset, line in enumerate(lines[:limit]):
            self.text(y + offset, x, line, style, width)
        return len(lines)

    def button(self, y, x, label, selected=False, width=None, danger=False, primary=False):
        width = width or cells(label) + 4
        active = 'primary' if primary else 'focus'
        self.fill(y, x, width, style=active if selected else 'text')
        self.text(y, x, ('› ' if selected else '  ') + label,
                  active if selected else 'danger' if danger else 'muted', width)

    def to_html(self, title='Private XUI · TUI 设计预览'):
        rows = []
        for row in self.grid:
            spans = []
            for cell in row:
                if cell.char:
                    spans.append('<span class="%s" style="width:%dch">%s</span>' %
                                 (cell.style, max(1, cells(cell.char)), html.escape(cell.char)))
            rows.append('<div class="row">' + ''.join(spans) + '</div>')
        colors = '\n'.join('.%s{color:%s;background:%s;font-weight:%s}' %
                           (name, fg, bg, '600' if bold else '400') for name, (fg, bg, bold) in PALETTE.items())
        return '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>' + html.escape(title) + '''</title>
<style>*{box-sizing:border-box}body{margin:0;background:#0a0a0a;color:#e5e5e5}main{width:max-content;
font-family:"DejaVu Sans Mono","Noto Sans Mono CJK SC",monospace;font-size:16px;line-height:20px;
font-variant-ligatures:none;white-space:pre}.row{height:20px;display:flex}.row span{display:inline-block;
flex:none;overflow:hidden;height:20px}''' + colors + '</style><main aria-label="终端设计预览">' + ''.join(rows) + '</main></html>'


@dataclass
class Session:
    screen: str = 'home'
    state: str = 'normal'
    period: int = 0
    selected: int = -1
    offset: int = 0
    paused: bool = False
    query: str = ''
    message: str = ''
    previous: str = 'home'
    help_return: tuple = ('home', 1)
    stack: list = field(default_factory=list)
    form_step: int = 0
    edit: str = '203.0.113.10'
    cursor: int = 12
    values: list = field(default_factory=lambda: ['203.0.113.10', 'node.example.com', 'sub.example.com', '1,2,3', 'Private XUI'])
    error: str = ''
    reveal: bool = False
    confirm_kind: str = 'uninstall'
    detail_kind: str = 'check'
    entry_mode: str = '自动优选 · 动态生成'
    pending_mode: str = ''
    token_version: int = 1

    def __post_init__(self):
        if self.selected < 0:
            self.selected = (0 if self.state == 'empty' else 2 if self.state in ('error', 'warning') else 1) if self.screen == 'home' else 0


DOMAIN_DATA = [('api.github.com', 186), ('www.google.com', 142), ('registry.npmjs.org', 94),
               ('cdn.jsdelivr.net', 78), ('www.wikipedia.org', 61), ('fonts.gstatic.com', 48),
               ('raw.githubusercontent.com', 43), ('www.youtube.com', 39), ('pypi.org', 32),
               ('www.cloudflare.com', 27), ('api.example.com', 23), ('images.unsplash.com', 17),
               ('www.reddit.com', 14), ('developer.mozilla.org', 12), ('time.example.net', 8)]
IP_RECORDS = 21
MAINTENANCE = [('update', '更新订阅服务', '更新 Worker，保留节点与现有入口'),
               ('settings', '订阅设置', '显示名称、自动优选与自有入口'),
               ('check', '连接与凭据检查', '查看回源、环境与凭据的演示结果'),
               ('panel', '查看面板访问', '查看 SSH 隧道示例与访问说明'),
               ('collector', 'Dashboard 采集', '流量、日志与保留时间'),
               ('rotate', '更换订阅令牌', '旧链接失效，客户端需要重新导入'),
               ('uninstall', '卸载 / 清理部署', '只清理本项目，保留面板与其他节点')]
FIELDS = [('服务器公网 IPv4', '使用文档示例地址；本预览不会连接服务器。'),
          ('节点域名', '用于节点回源，例如 node.example.com。'),
          ('订阅域名', '应与节点域名不同，例如 sub.example.com。'),
          ('启用协议', '1 VLESS   2 Trojan   3 VMess；用逗号分隔。'),
          ('订阅显示名称', '导入 Clash / Mihomo 后显示的名称。')]


def notice(session):
    return {
        'normal': ('采集正常', '仅覆盖启用后的已采集时段', 'muted'),
        'warning': ('自动优选未启用', '当前仅节点域名；A 打开设置', 'warning'),
        'error': ('采集失败', '检查服务与磁盘空间；R 重试', 'danger'),
        'empty': ('等待首次采样', '尚未建立流量基线', 'muted'),
        'stale': ('采样已过期', '显示上次数据；最后采样 8 分钟前', 'warning'),
        'no-log': ('访问日志不可读', '流量仍可查看，连接记录不可用', 'warning'),
        'gap': ('存在采样中断', '断点代表未知时段，未补算为零', 'warning'),
    }[session.state]


def header(canvas, session):
    w, h = canvas.width, canvas.height
    canvas.text(1, 2, 'Private XUI', 'strong')
    badge = '设计预览 · 演示数据'
    canvas.text(1, w - cells(badge) - 2, badge, 'muted')
    title = TITLES.get(session.screen, '控制台')
    if session.screen == 'detail':
        title = {'counters': '当前累计计数', 'panel': '面板访问'}.get(session.detail_kind, '检查结果')
    canvas.text(2, 2, '/ ' + title, 'muted')
    status = 'S 状态 / ' + STATE_NAMES[session.state]
    canvas.text(2, w - cells(status) - 2, status, 'dim')
    canvas.line(3, 2, w - 4)
    canvas.line(h - 3, 2, w - 4)
    if session.message:
        canvas.text(h - 4, 2, session.message, 'blue', w - 4)
    keys = {'home': '←→ 选择   Enter 打开   1–4 操作   D 流量   S 状态   ? 帮助',
            'dashboard': '1–3 页面   ←→ 时段   R 刷新   P 暂停   C 累计   Esc 返回   ? 帮助',
            'domains': '1–3 页面   ←→ 时段   ↑↓ 滚动   / 搜索   Esc 返回   ? 帮助',
            'history': '1–3 页面   ←→ 时段   ↑↓ 滚动   / 搜索   Esc 返回   ? 帮助',
            'form': 'Enter 下一步   ←→ 移动   Ctrl+U 清空   Esc 返回   ? 帮助',
            'search': 'Enter 搜索   Ctrl+U 清空   Esc 返回   ? 帮助',
            'subscription': 'Enter 显示 / 隐藏地址   E 设置   Esc 返回   ? 帮助',
            'confirm': '↑↓ 阅读摘要   ←→ 选择   Enter 确认   Esc 返回   ? 帮助'}
    compact = {'home': '1–4 操作  D 流量  S 状态  ? 帮助', 'dashboard': '1–3 页 ←→ 时段 C 累计 Esc 返回 ? 帮助',
               'domains': '1–3 页 ↑↓ 滚动 / 搜索 Esc 返回 ? 帮助', 'history': '1–3 页 ↑↓ 滚动 / 搜索 Esc 返回 ? 帮助',
               'form': 'Enter 下一步 ←→ 移动 Esc 返回 ? 帮助', 'subscription': 'Enter 显示地址 E 设置 Esc 返回 ? 帮助',
               'confirm': '↑↓ 摘要 ←→ 选择 Enter 确认 Esc 返回 ? 帮助'}
    footer = keys.get(session.screen, '↑↓ 选择   Enter 打开   Esc 返回   S 状态   ? 帮助')
    if w < 80:
        footer = compact.get(session.screen, '↑↓ / ←→ 选择 Enter 确认 Esc 返回 ? 帮助')
    canvas.text(h - 2, 2, footer, 'muted', w - 4)


def home(canvas, session):
    w, h = canvas.width, canvas.height
    waiting = session.state == 'empty'
    issue = session.state in ('warning', 'error', 'empty')
    heading = '开始你的首次部署' if waiting else '3x-ui 服务未运行' if session.state == 'error' else '自动优选未启用' if issue else '订阅已配置'
    hint = '填写配置后，先检查摘要再确认。' if waiting else '进入维护，查看状态与恢复路径。' if session.state == 'error' else '当前仅域名入口；A 打开设置。' if issue else '动态优选 · 数量以客户端更新后的订阅为准。'
    server = [('系统', 'Ubuntu 22.04 LTS'), ('IPv4', session.values[0]), ('回源', '17001 / 17002 / 17003')]
    deployment = [('节点', '未配置' if waiting else session.values[1]), ('订阅', '未配置' if waiting else session.values[2]), ('入口', '待配置' if waiting else '仅节点域名' if session.state == 'warning' else session.entry_mode)]
    if w >= 80 and h >= 24:
        top, ch = (7, 8) if h >= 34 else (5, 7)
        card = (w - 6) // 2
        for x, title, rows in [(2, '服务器 / 3x-ui', server), (4 + card, '节点 / 订阅', deployment)]:
            canvas.box(top, x, card, ch, title)
            status = ('○ 尚未部署' if waiting else '● 已部署 · 本机记录') if x > 2 else ('● 服务未运行' if session.state == 'error' else '● 已安装 · 运行中')
            canvas.text(top + 1, x + 2, status, 'danger' if session.state == 'error' and x == 2 else 'strong')
            for index, (label, value) in enumerate(rows):
                canvas.text(top + 3 + index, x + 2, label, 'muted')
                canvas.text(top + 3 + index, x + 8, value, 'text', card - 10)
        y = top + ch + (2 if h >= 34 else 1)
        canvas.box(y, 2, w - 4, 4 if h >= 34 else 3)
        canvas.text(y + 1, 4, heading, 'warning' if issue else 'strong')
        if h >= 34:
            canvas.text(y + 2, 4, hint, 'muted')
        else:
            canvas.text(y + 1, 4 + cells(heading) + 3, hint, 'muted', w - cells(heading) - 11)
        ay = h - 10 if h >= 34 else h - 6
        aw = (w - 7) // 4
        for index, title in enumerate(('部署', '订阅', '维护', '退出')):
            x = 2 + index * (aw + 1)
            canvas.button(ay, x, '%s  %s' % (index + 1, title), session.selected == index, aw)
            if h >= 34:
                canvas.text(ay + 2, x + 2, ('首次安装', '查看完整地址', '检查与设置', '服务继续运行')[index], 'dim', aw - 2)
        if h >= 34:
            canvas.text(5, 2, '服务与配置', 'strong')
            canvas.text(h - 6, 2, '部署状态来自本机记录；实际连通性可在维护中检查。', 'dim')
    else:
        rows = [('3x-ui', '服务未运行' if session.state == 'error' else '已安装 · 运行中'), ('部署', '尚未部署' if waiting else '已部署 · 本机记录')] + deployment
        for index, (label, value) in enumerate(rows):
            canvas.text(5 + index, 2, label, 'muted')
            canvas.text(5 + index, 10, value, 'text', w - 12)
        canvas.text(11, 2, heading, 'warning' if issue else 'strong')
        canvas.text(12, 2, hint, 'muted', w - 4)
        for index, title in enumerate(('部署', '订阅', '维护', '退出')):
            canvas.button(h - 6 + index // 2, 2 + index % 2 * ((w - 4) // 2), '%s %s' % (index + 1, title), session.selected == index, (w - 6) // 2)


def toolbar(canvas, session):
    w = canvas.width
    x = 2
    tabs_y = 4 if canvas.height <= 20 else 5
    for target, label in [('dashboard', '1 概览'), ('domains', '2 热门域名'), ('history', '3 连接历史')]:
        canvas.button(tabs_y, x, label, session.screen == target)
        x += cells(label) + 6
    x = 2
    for index, label in enumerate(('24 小时', '7 天', '30 天')):
        canvas.text(7 if canvas.height >= 26 else tabs_y + 1, x, '[' + label + ']' if index == session.period else ' ' + label + ' ', 'blue' if index == session.period else 'dim')
        x += cells(label) + 4
    label, description, style = notice(session)
    y = 9 if canvas.height >= 26 else tabs_y + 2
    canvas.text(y, 2, ('已暂停刷新 · ' if session.paused else '') + label + ' · ' + description, style, w - 4)
    if w >= 100:
        canvas.text(7, w - 28, '30 秒采样 / 5 秒刷新', 'dim')
    return y + 2


def spark(session, width, scale=1):
    if session.state in ('empty', 'error'):
        return '等待采样数据' if session.state == 'empty' else '采集暂不可用'
    blocks, result = '▁▂▃▄▅▆▇█', ''
    for index in range(width):
        if session.state == 'gap' and width * .42 < index < width * .51:
            result += '·'
        else:
            value = math.sin(index * .24 + session.period) * .18 + math.sin(index * .071 + scale) * .21 + .5
            result += blocks[max(0, min(7, round(value * 7)))]
    return result


def dashboard(canvas, session):
    w, h = canvas.width, canvas.height
    y = toolbar(canvas, session)
    multiplier = (1, 6.6, 24.4)[session.period]
    missing = session.state in ('empty', 'error')
    values = ['—' if missing else '%.1f GiB' % (25.6 * multiplier), '—' if missing else '%.1f GiB' % (4.4 * multiplier),
              '—' if session.state in ('empty', 'error', 'no-log') else format(sum(round(n * multiplier) for _, n in DOMAIN_DATA) + round(IP_RECORDS * multiplier), ','), '—' if missing else '230.1 KiB/s']
    labels = ('下载 / 所选时段', '上传 / 所选时段', '连接记录', '近两分钟下载均速')
    if h >= 34 and w >= 100:
        cw = (w - 7) // 4
        for index, (label, value) in enumerate(zip(labels, values)):
            x = 2 + index * (cw + 1)
            canvas.box(y, x, cw, 5, label)
            canvas.text(y + 2, x + 2, value, 'strong', cw - 4)
        cy, cw = y + 7, (w - 7) * 2 // 3
        canvas.box(cy, 2, cw, 10, '流量趋势')
        canvas.text(cy + 2, 4, '下载', 'muted')
        canvas.text(cy + 3, 4, spark(session, cw - 4), 'chart', cw - 4)
        canvas.text(cy + 5, 4, '上传', 'muted')
        canvas.text(cy + 6, 4, spark(session, cw - 4, 3), 'chart2', cw - 4)
        canvas.text(cy + 8, 4, ('24 小时前', '7 天前', '30 天前')[session.period], 'dim')
        canvas.text(cy + 8, cw - 5, '现在', 'dim')
        x, rw = cw + 4, w - cw - 6
        canvas.box(cy, x, rw, 10, '节点当前累计')
        for index, (name, total) in enumerate([('VLESS', '↑ 4.4 / ↓ 25.6 GiB'), ('TROJAN', '↑ 1.5 / ↓ 8.1 GiB'), ('VMESS', '↑ 0.2 / ↓ 3.2 GiB')]):
            canvas.text(cy + 2 + index * 2, x + 2, name, 'muted')
            canvas.text(cy + 3 + index * 2, x + 2, '—' if missing else total, 'text', rw - 4)
        canvas.text(h - 5, 2, '趋势独立缩放 · 点号为无采样 · 当前累计可由面板重置', 'dim', w - 4)
    else:
        for index in range(2):
            x = 2 + index * ((w - 4) // 2)
            canvas.text(y, x, labels[index], 'muted')
            canvas.text(y + 1, x, values[index], 'strong')
        canvas.text(y + 3, 2, '连接 ' + values[2], 'muted')
        canvas.text(y + 3, w // 2, '2 分钟均速 ' + ('230 KiB/s' if not missing else '—'), 'muted', w // 2 - 2)
        cy = y + (4 if h <= 20 else 5)
        if cy + 2 <= h - 5:
            canvas.text(cy, 2, '历史趋势 · 独立缩放 / · 无采样', 'dim', w - 4)
            canvas.text(cy + 1, 2, '下 ' + spark(session, w - 7), 'chart', w - 4)
            canvas.text(cy + 2, 2, '上 ' + spark(session, w - 7, 3), 'chart2', w - 4)


def listings(canvas, session):
    w, h = canvas.width, canvas.height
    y = toolbar(canvas, session)
    if session.state in ('error', 'empty', 'no-log'):
        heading = {'error': '统计暂不可用', 'empty': '尚无连接记录', 'no-log': '访问日志不可读'}[session.state]
        canvas.text(y + 2, 4, heading, 'strong')
        canvas.paragraph(y + 4, 4, '启用后的目标域名 / IP 会显示在这里。' if session.state == 'empty' else '检查采集服务与日志路径后重试。流量统计与连接历史分别显示状态。', w - 8, limit=max(1, h - y - 9))
        return
    if session.screen == 'domains':
        canvas.text(y, 2, '#', 'dim')
        canvas.text(y, 7, '目标域名', 'muted')
        canvas.text(y, w - 17, '连接次数', 'muted')
        canvas.line(y + 1, 2, w - 4)
        room = max(1, h - y - 7)
        session.offset = min(session.offset, max(0, len(DOMAIN_DATA) - room))
        for index, (target, count) in enumerate(DOMAIN_DATA[session.offset:session.offset + room], session.offset):
            row = y + 2 + index - session.offset
            canvas.text(row, 2, '%02d' % (index + 1), 'dim')
            canvas.text(row, 7, target, 'text', w - 26)
            canvas.text(row, w - 17, str(round(count * (1, 6.6, 24.4)[session.period])).rjust(7), 'strong')
            if w >= 90:
                canvas.text(row, w - 8, '━' * max(1, round(count / 186 * 5)), 'chart2')
        ip_count = round(IP_RECORDS * (1, 6.6, 24.4)[session.period])
        note = '仅 IP %s 条 · 含接受/拒绝 · 非浏览量' % ip_count if w < 80 else '仅 IP %s 条（未参与域名排名） · 包含接受与拒绝 · 不是网页浏览量' % ip_count
        canvas.text(h - 5, 2, note, 'dim', w - 4)
    else:
        canvas.text(y, 2, '搜索 / ' + (session.query or '全部目标'), 'muted', w - 4)
        y += 2
        tw = 13 if w >= 80 else 7
        target_width = w - tw - 19
        for x, label in [(2, '时间'), (2 + tw, '目标:端口'), (w - 15, '协议'), (w - 7, '结果')]:
            canvas.text(y, x, label, 'muted')
        canvas.line(y + 1, 2, w - 4)
        records = [(index, DOMAIN_DATA[index % len(DOMAIN_DATA)][0]) for index in range(100) if session.query.lower() in DOMAIN_DATA[index % len(DOMAIN_DATA)][0].lower()]
        room = max(1, h - y - 7)
        session.offset = min(session.offset, max(0, len(records) - room))
        for row, (index, target) in enumerate(records[session.offset:session.offset + room], y + 2):
            stamp = dt.datetime(2026, 10, 7, 14, 58) - dt.timedelta(minutes=index)
            canvas.text(row, 2, stamp.strftime('%m-%d %H:%M' if w >= 80 else '%H:%M'), 'dim')
            canvas.text(row, 2 + tw, target + ':443', 'text', target_width)
            canvas.text(row, w - 15, ('VLESS', 'TROJAN', 'VMESS')[index % 3], 'muted', 6)
            canvas.text(row, w - 7, '拒绝' if index % 11 == 4 else '接受', 'warning' if index % 11 == 4 else 'muted')
        if not records:
            canvas.text(y + 3, 2, '暂无匹配记录；/ 修改或清空搜索。', 'muted', w - 4)
        canvas.text(h - 5, 2, '最近 100 条匹配记录 · 服务器本地时间', 'dim', w - 4)


def maintenance(canvas, session):
    w, h = canvas.width, canvas.height
    roomy = h >= 34
    y = 5
    canvas.text(y, 2, '配置与诊断', 'muted')
    y += 2 if roomy else 1
    for index, (_, title, description) in enumerate(MAINTENANCE):
        if index == 5:
            y += 1
            canvas.text(y, 2, '需要确认的操作', 'muted')
            y += 2 if roomy else 1
        canvas.button(y, 2, title, session.selected == index, w - 4, index >= 5)
        if roomy:
            canvas.text(y + 1, 4, description, 'dim')
        y += 3 if roomy else 1
    if not session.message and h >= 24:
        canvas.text(h - 4, 2, MAINTENANCE[session.selected][2], 'muted', w - 4)


def subscription(canvas, session):
    w, h = canvas.width, canvas.height
    canvas.text(5, 2, session.values[4], 'strong', w - 4)
    canvas.text(6, 2, 'Clash / Mihomo · 完整 YAML 配置', 'muted', w - 4)
    canvas.line(8, 2, w - 4)
    canvas.text(9, 2, '订阅域名', 'muted')
    canvas.text(9, 16, session.values[2], 'text', w - 18)
    canvas.text(10, 2, '入口方式', 'muted')
    canvas.text(10, 16, '仅节点域名' if session.state == 'warning' else session.entry_mode, 'text', w - 18)
    if session.reveal:
        canvas.paragraph(12, 2, 'https://sub.example.com/s/demo-only-%02d/Private-XUI.yaml' % session.token_version, w - 4, 'blue')
    else:
        canvas.text(12, 2, 'https://sub.example.com/s/••••/Private-XUI.yaml', 'dim', w - 4)
    if h >= 26:
        canvas.paragraph(15, 2, '在客户端导入完整订阅地址。入口数量随地址池变化，多个入口共用同一台 VPS。', min(w - 4, 80))
        canvas.text(19, 2, '链接包含访问权限；此处仅为不可用的演示地址。', 'dim', w - 4)
    canvas.button(h - 6, 2, '隐藏地址' if session.reveal else '显示完整地址', True)
    canvas.text(h - 5, 2, '完整地址显示后，可在终端手动选择复制。', 'dim', w - 4)


def form(canvas, session):
    w, h = canvas.width, canvas.height
    search = session.screen == 'search'
    title, hint = ('目标域名或 IP', '清空后回车，显示全部连接记录。') if search else FIELDS[session.form_step]
    canvas.text(5, 2, '搜索连接历史' if search else '部署配置  /  %d · 5' % (session.form_step + 1), 'strong')
    canvas.text(7, 2, title, 'muted')
    canvas.box(9, 2, w - 4, 3, style='blue')
    canvas.fill(10, 3, w - 6, style='field')
    start = 0
    session.cursor = min(len(session.edit), max(0, session.cursor))
    while cells(session.edit[start:session.cursor]) > w - 10:
        start += 1
    visible = session.edit[start:]
    canvas.text(10, 4, visible, 'field', w - 8)
    cursor_x = min(w - 5, 4 + cells(session.edit[start:session.cursor]))
    canvas.cursor = (10, cursor_x)
    canvas.grid[10][cursor_x].style = 'cursor'
    canvas.paragraph(13, 2, hint, w - 4, limit=max(1, h - 18))
    if session.error:
        canvas.text(h - 5, 2, session.error, 'danger', w - 4)


def confirm(canvas, session):
    w, h = canvas.width, canvas.height
    kind = session.confirm_kind
    title, lines = {
        'uninstall': ('卸载本项目', ['清理本项目节点、Worker、DNS 与回源规则。', '停止 Dashboard 采集；统计历史保留。', '保留 3x-ui 面板与其他节点。']),
        'rotate': ('更换订阅令牌', ['旧订阅链接将失效。', '完成后需使用新地址重新导入客户端。', '现有节点配置与统计历史保留。']),
        'update': ('更新订阅服务', ['更新本项目的订阅 Worker。', '保留节点、订阅名称与现有优选入口。']),
        'install': ('确认部署配置', [label + '  ' + value for label, value in zip(('IPv4', '节点', '订阅', '协议', '名称'), session.values)]),
        'settings': ('更新订阅设置', ['入口方式：' + (session.pending_mode or '自动优选'), '实际产品需要确认后才会更新 Worker。']),
        'stop': ('停止并卸载采集', ['停止采集，统计历史保留。', '按需恢复自己修改的日志字段。', '实际操作可能短暂重启 x-ui。']),
        'collect': ('启用后台采集', ['记录本项目流量与目标域名 / IP。', '需要开启访问日志时将重启 x-ui，短暂断线。', 'TUI 只统计本项目，原始日志可能包含其他入站。']),
    }[kind]
    canvas.text(5, 2, title, 'danger' if kind in ('uninstall', 'rotate', 'stop') else 'strong')
    summary = []
    for line in lines:
        summary.extend(wrapped(line, w - 4))
        if h >= 26:
            summary.append('')
    room = h - 15
    session.offset = min(session.offset, max(0, len(summary) - room))
    for index, line in enumerate(summary[session.offset:session.offset + room], 7):
        canvas.text(index, 2, line, 'text', w - 4)
    if len(summary) > room:
        canvas.text(h - 8, 2, '↑↓ 阅读摘要  %d–%d / %d 行' % (session.offset + 1, min(len(summary), session.offset + room), len(summary)), 'blue', w - 4)
    canvas.text(h - 7, 2, '本次仅演示确认，不执行服务器操作。', 'dim', w - 4)
    canvas.button(h - 5, 2, '返回', session.selected == 0, 14)
    canvas.button(h - 5, 19, '确认并继续', session.selected == 1, 24, kind in ('uninstall', 'rotate', 'stop'), primary=True)


def extras(canvas, session):
    w, h = canvas.width, canvas.height
    if session.screen == 'help':
        lines = ['导航  /  Esc 返回，Q 退出当前页', '首页  /  1 部署  2 订阅  3 维护  4 退出', 'Dashboard  /  D 打开，1–3 切换页签', '← →  /  切换 24 小时、7 天、30 天', '↑ ↓  /  选择操作或滚动记录', '/ 搜索  /  R 刷新  /  P 暂停显示刷新', 'C 累计计数  /  S 切换演示状态', '?  /  关闭帮助；退出不停止后台采集', '所有数据为演示，所有操作只改变预览。']
    elif session.screen == 'settings':
        canvas.text(5, 2, '订阅入口方式', 'strong')
        options = ['保留当前入口', '自动优选（推荐）', '内置 Cloudflare 候选', '仅节点域名', '自有地址文件（演示）']
        for index, label in enumerate(options):
            canvas.button(7 + index, 2, label, session.selected == index, w - 4)
        canvas.text(h - 5, 2, '候选入口需要客户端测速。Enter 查看确认。', 'dim', w - 4)
        return
    elif session.screen == 'collector':
        lines = ['采集服务  /  已启用（演示）', '采样间隔  /  30 秒', '保留时间  /  30 天，详细连接最多 10 万条', '数据仅覆盖启用后的已采集时段。', '', 'O  打开 Dashboard', 'I  启用 / 更新采集', 'X  停止并卸载采集，保留历史']
    elif session.detail_kind == 'counters':
        lines = ['节点当前累计计数', 'VLESS   ↑ 4.4 GiB  /  ↓ 25.6 GiB', 'TROJAN  ↑ 1.5 GiB  /  ↓ 8.1 GiB', 'VMESS   ↑ 0.2 GiB  /  ↓ 3.2 GiB', '', '这些是 3x-ui 当前计数，可由面板重置。', '与 Dashboard 的所选时段流量分别展示。']
        if session.state in ('error', 'empty'):
            lines[1:4] = ['VLESS   —', 'TROJAN  —', 'VMESS   —']
    elif session.detail_kind == 'panel':
        lines = ['面板访问  /  SSH 隧道示例', '面板仅监听服务器本机。', '在你的电脑终端执行：', 'ssh -L 2053:127.0.0.1:2053', '    root@203.0.113.10', '再访问 http://127.0.0.1:2053', '', '文档示例地址，未读取任何真实面板凭据。']
    else:
        lines = ['连接与凭据检查  /  演示结果', '系统环境     支持', '本机部署     已记录', '回源握手     演示通过', 'Cloudflare   演示可读', '', '只读检查的成功，不代表所有写入权限均通过。', '预览没有发出网络请求。R 可重看此演示。']
    y = 5
    for line in lines:
        y += canvas.paragraph(y, 2, line, w - 4, 'strong' if y == 5 else 'muted')
        if h >= 30:
            y += 1


def render(session, width=120, height=36):
    canvas = Canvas(width, height)
    if width < 52 or height < 20:
        canvas.text(0, 0, '设计预览 · 演示数据', 'strong')
        canvas.text(2, 0, '请扩大终端至 52 列 × 20 行。', 'warning')
        canvas.text(4, 0, 'Q 退出', 'muted')
        return canvas
    header(canvas, session)
    renderer = {'home': home, 'dashboard': dashboard, 'domains': listings, 'history': listings,
                'maintenance': maintenance, 'subscription': subscription, 'form': form, 'search': form,
                'confirm': confirm}.get(session.screen, extras)
    renderer(canvas, session)
    return canvas


def navigate(session, screen, selected=0):
    session.screen, session.selected, session.offset, session.message = screen, selected, 0, ''


def open_page(session, screen, selected=0):
    session.stack.append((session.screen, session.selected))
    navigate(session, screen, selected)


def go_back(session):
    target, selected = session.stack.pop() if session.stack else ('home', 1)
    navigate(session, target, selected)


def ask_confirmation(session, kind):
    session.previous, session.confirm_kind = session.screen, kind
    open_page(session, 'confirm')


def handle(session, key):
    """Apply one key to in-memory state. Return False only to close this preview."""
    screen = session.screen
    if key == '?':
        if screen == 'help':
            navigate(session, *session.help_return)
        else:
            session.help_return = (screen, session.selected)
            navigate(session, 'help')
        return True
    if key in ('\x1b', 'q', 'Q') and screen not in ('form', 'search'):
        if screen == 'home':
            return False
        if screen == 'help':
            navigate(session, *session.help_return)
        else:
            go_back(session)
        return True
    if key in ('s', 'S') and screen not in ('form', 'search'):
        session.state = STATES[(STATES.index(session.state) + 1) % len(STATES)]
        session.message = ''
        if screen == 'home':
            session.selected = 0 if session.state == 'empty' else 2 if session.state in ('warning', 'error') else 1
        return True
    enter = key in ('\n', '\r', curses.KEY_ENTER)
    backward = key in (curses.KEY_UP, curses.KEY_LEFT, 'k', 'h')
    forward = key in (curses.KEY_DOWN, curses.KEY_RIGHT, 'j', 'l', '\t')
    if screen in ('form', 'search'):
        if key == '\x1b':
            go_back(session)
        elif enter:
            if screen == 'search':
                session.query = session.edit.strip()
                go_back(session)
                navigate(session, 'history')
            elif not session.edit.strip():
                session.error = '此项不能为空。'
            else:
                session.values[session.form_step] = session.edit.strip()
                if session.form_step == 4:
                    ask_confirmation(session, 'install')
                else:
                    session.form_step += 1
                    session.edit = session.values[session.form_step]
                    session.cursor, session.error = len(session.edit), ''
        elif key in (curses.KEY_BACKSPACE, '\x7f', '\b') and session.cursor:
            session.edit = session.edit[:session.cursor - 1] + session.edit[session.cursor:]
            session.cursor -= 1
        elif key == '\x15':
            session.edit, session.cursor = '', 0
        elif key == curses.KEY_LEFT:
            session.cursor = max(0, session.cursor - 1)
        elif key == curses.KEY_RIGHT:
            session.cursor = min(len(session.edit), session.cursor + 1)
        elif isinstance(key, str) and key.isprintable() and len(session.edit) < 120:
            session.edit = session.edit[:session.cursor] + key + session.edit[session.cursor:]
            session.cursor += 1
        return True
    if screen == 'home':
        if backward or forward:
            session.selected = (session.selected + (1 if forward else -1)) % 4
        if key in ('d', 'D'):
            open_page(session, 'dashboard')
        elif key in ('a', 'A'):
            open_page(session, 'settings', 1)
        elif enter or key in ('1', '2', '3', '4'):
            choice = session.selected if enter else int(key) - 1
            if choice == 3:
                return False
            open_page(session, ('form', 'subscription', 'maintenance')[choice])
            if choice == 0:
                session.form_step, session.edit = 0, session.values[0]
                session.cursor = len(session.edit)
    elif screen in ('dashboard', 'domains', 'history'):
        if key in ('1', '2', '3'):
            navigate(session, ('dashboard', 'domains', 'history')[int(key) - 1])
        elif key in (curses.KEY_LEFT, curses.KEY_RIGHT):
            session.period = (session.period + (1 if key == curses.KEY_RIGHT else -1)) % 3
            session.offset, session.paused = 0, False
        elif key in (curses.KEY_DOWN, 'j'):
            session.offset = min(14 if screen == 'domains' else 99, session.offset + 1)
        elif key in (curses.KEY_UP, 'k'):
            session.offset = max(0, session.offset - 1)
        elif key in ('p', 'P'):
            session.paused = not session.paused
        elif key in ('r', 'R'):
            session.paused, session.message = False, '已刷新演示数据。'
        elif key in ('c', 'C'):
            session.detail_kind = 'counters'
            open_page(session, 'detail')
        elif key in ('a', 'A'):
            open_page(session, 'settings', 1)
        elif key == '/':
            open_page(session, 'search')
            session.edit, session.cursor = session.query, len(session.query)
    elif screen == 'maintenance':
        if backward or forward:
            session.selected = (session.selected + (1 if forward else -1)) % len(MAINTENANCE)
        elif enter:
            action = MAINTENANCE[session.selected][0]
            if action in ('update', 'rotate', 'uninstall'):
                ask_confirmation(session, action)
            elif action in ('settings', 'collector'):
                open_page(session, action)
            else:
                session.detail_kind = action
                open_page(session, 'detail')
    elif screen == 'subscription':
        if enter:
            session.reveal = not session.reveal
        elif key in ('e', 'E'):
            open_page(session, 'settings')
    elif screen == 'settings':
        if backward or forward:
            session.selected = (session.selected + (1 if forward else -1)) % 5
        elif enter:
            session.pending_mode = ('保留当前入口', '自动优选 · 动态生成', '内置 Cloudflare 候选', '仅节点域名', '自有地址文件')[session.selected]
            ask_confirmation(session, 'settings')
    elif screen == 'confirm':
        if key in (curses.KEY_UP, 'k', curses.KEY_DOWN, 'j'):
            session.offset = max(0, session.offset + (1 if key in (curses.KEY_DOWN, 'j') else -1))
        elif key in (curses.KEY_LEFT, 'h', curses.KEY_RIGHT, 'l', '\t'):
            session.selected = 1 - session.selected
        elif enter:
            accepted = session.selected == 1
            go_back(session)
            if accepted and session.confirm_kind == 'settings' and session.pending_mode != '保留当前入口':
                session.entry_mode = session.pending_mode
                session.state = 'warning' if session.entry_mode == '仅节点域名' else 'normal'
            if accepted and session.confirm_kind == 'rotate':
                session.token_version += 1
            if accepted and session.confirm_kind == 'uninstall':
                session.state = 'empty'
            if accepted and session.confirm_kind == 'install':
                session.stack.clear()
                session.state = 'normal'
                navigate(session, 'home', 1)
            session.message = '已演示确认 · 未执行任何服务器操作。' if accepted else '已返回，配置保持不变。'
    elif screen == 'collector':
        if key in ('o', 'O'):
            open_page(session, 'dashboard')
        elif key in ('i', 'I', 'x', 'X'):
            ask_confirmation(session, 'collect' if key.lower() == 'i' else 'stop')
    elif screen == 'detail' and key in ('r', 'R', '\n', '\r'):
        session.message = '已重新显示演示结果 · 没有网络请求。'
    return True


def curses_app(screen, session, no_color=False):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.keypad(True)
    colored = curses.has_colors() and not no_color
    if colored:
        curses.start_color()
        curses.use_default_colors()
    attributes = {}
    color_map = {'#e5e5e5': 254, '#fafafa': 255, '#a3a3a3': 247, '#737373': 243, '#333333': 236,
                 '#60a5fa': 75, '#93c5fd': 111, '#ffffff': 255, '#1e40af': 25, '#262626': 235,
                 '#86b99a': 108, '#d4b678': 180, '#e59b9b': 181, '#d4d4d4': 252,
                 '#f5f5f5': 255, '#171717': 234, '#0a0a0a': 232}
    for index, (name, (fg, bg, bold)) in enumerate(PALETTE.items(), 1):
        if not colored:
            attributes[name] = (curses.A_BOLD if bold else 0) | (curses.A_REVERSE if name in ('focus', 'primary', 'cursor') else 0)
            continue
        foreground = color_map[fg] if curses.COLORS >= 256 else curses.COLOR_BLUE if name == 'blue' else curses.COLOR_WHITE
        background = color_map[bg] if curses.COLORS >= 256 else curses.COLOR_BLUE if name in ('focus', 'primary') else curses.COLOR_BLACK
        curses.init_pair(index, foreground, background)
        attributes[name] = curses.color_pair(index) | (curses.A_BOLD if bold else 0)
    while True:
        height, width = screen.getmaxyx()
        canvas = render(session, width, height)
        screen.erase()
        for y, row in enumerate(canvas.grid):
            for x, cell in enumerate(row):
                if cell.char:
                    try:
                        screen.addstr(y, x, cell.char, attributes[cell.style])
                    except curses.error:
                        pass
        try:
            curses.curs_set(1 if canvas.cursor else 0)
            if canvas.cursor:
                screen.move(*canvas.cursor)
        except curses.error:
            pass
        screen.refresh()
        try:
            key = screen.get_wch()
        except curses.error:
            continue
        if key == '\x03':
            break
        if width < 52 or height < 20:
            if key in ('\x1b', 'q', 'Q'):
                break
            continue
        if not handle(session, key):
            break


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--screen', choices=SCREENS, default='home')
    parser.add_argument('--state', choices=STATES, default='normal')
    parser.add_argument('--size', default='120x36', help='Export dimensions in columns x rows')
    parser.add_argument('--no-color', action='store_true', help='Use monochrome focus and borders; also respects NO_COLOR')
    parser.add_argument('--export-dir', type=Path, help='Export all eight HTML screens; do not start curses')
    args = parser.parse_args()
    try:
        width, height = map(int, args.size.lower().split('x'))
    except ValueError:
        parser.error('--size must look like 120x36')
    if width < 1 or height < 1:
        parser.error('--size dimensions must be positive')
    if args.export_dir:
        args.export_dir.mkdir(parents=True, exist_ok=True)
        for name in SCREENS:
            session = Session(screen=name, state=args.state)
            (args.export_dir / (name + '.html')).write_text(render(session, width, height).to_html(TITLES[name] + ' · 设计预览'), encoding='utf-8')
        print('Exported 8 design previews to %s' % args.export_dir)
    else:
        curses.wrapper(curses_app, Session(screen=args.screen, state=args.state), args.no_color or 'NO_COLOR' in os.environ)


if __name__ == '__main__':
    main()
