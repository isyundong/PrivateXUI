import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import manage
import tui
import wizard

REPORT = {'system': 'Ubuntu 22.04 LTS', 'ipv4': '1.1.1.1', 'supported': True,
          'architecture': 'x86_64', 'python': '3.10', 'issues': [], 'ip_checked': True}
STATE = {'version': 1, 'deployment_id': 'a' * 32, 'status': 'ready', 'domain': 'node.example.com',
         'subscription_domain': 'sub.example.com', 'subscription_name': '我的订阅',
         'subscription_token': 'private-subscription-token', 'uuid': 'private-node-credential',
         'routes': [{'protocol': protocol, 'port': port} for protocol, port in
                    [('vless', 17001), ('trojan', 17002), ('vmess', 17003)]],
         'preferred': [{'address': '1.1.1.1', 'name': 'test'}, {'address': '1.1.1.1', 'name': 'duplicate'}]}


class Screen:
    def __init__(self, keys, dimensions=(24, 80)):
        self.keys = iter(keys)
        self.dimensions = dimensions
        self.writes = []

    def keypad(self, value): pass
    def timeout(self, value): pass
    def erase(self): pass
    def clear(self): pass
    def refresh(self): pass
    def move(self, y, x): pass
    def getmaxyx(self): return self.dimensions
    def get_wch(self): return next(self.keys)
    def addstr(self, y, x, value, style=0):
        if x + tui.cell_width(value) >= self.dimensions[1]:
            raise AssertionError('Text overflows terminal columns')
        self.writes.append((y, x, value, style))


class TerminalTests(unittest.TestCase):
    def setUp(self):
        for target in ('curs_set', 'has_colors'):
            p = patch.object(tui.curses, target, return_value=False)
            p.start()
            self.addCleanup(p.stop)

    def ui(self, keys, dimensions=(24, 80)):
        screen = Screen(keys, dimensions)
        return tui.TerminalUI(screen), screen

    def test_cjk_width_and_escape_sanitization(self):
        self.assertEqual(tui.cell_width('A中e\u0301'), 4)
        self.assertEqual(tui.fit('A中文', 4), 'A中')
        self.assertEqual(tui.tail('A中文', 4), '中文')
        self.assertEqual(tui.elide('node.example.com', 8), 'node.ex…')
        self.assertNotIn('\x1b', tui.clean_text('\x1b[31mhello'))
        self.assertNotIn('\n', tui.clean_text('first\nsecond'))

    def test_arrow_navigation_and_scroll_select_stable_action(self):
        ui, screen = self.ui([tui.curses.KEY_DOWN] * 5 + ['\n'], (20, 52))
        choices = [('action-%s' % i, '操作%s' % i, '说明') for i in range(7)]
        self.assertEqual(ui.choose('菜单', choices, summary=['状态'] * 5), 'action-5')
        self.assertTrue(any('操作5' in row[2] for row in screen.writes))

    def test_number_shortcuts_and_escape(self):
        ui, _ = self.ui(['2', '\x1b'])
        choices = [('one', '一', ''), ('two', '二', '')]
        self.assertEqual(ui.choose('菜单', choices), 'two')
        self.assertIsNone(ui.choose('菜单', choices))

    def test_narrow_terminal_refuses_action_until_resize(self):
        ui, screen = self.ui(['1', '\x1b'], (10, 30))
        self.assertIsNone(ui.choose('菜单', [('one', '操作', '')]))
        self.assertTrue(any('终端太小' in row[2] for row in screen.writes))

    def test_home_has_bordered_cards_and_high_contrast_focus_at_multiple_sizes(self):
        model = {'server': {'title': '服务器 / 3x-ui', 'badge': ('已安装 · 运行中', 'good'),
                            'rows': [('系统', 'Ubuntu 22.04 LTS', ''), ('IPv4', '1.1.1.1', ''), ('回源', '17001 / 17002 / 17003', '')]},
                 'deployment': {'title': '节点 / 订阅', 'badge': ('已部署 · 本机记录', 'good'),
                                'rows': [('节点', 'node.example.com', ''), ('订阅', 'sub.example.com', ''), ('入口', '仅域名', 'warning')]},
                 'notice': {'title': '尚未启用自动优选', 'tone': 'warning',
                            'lines': ['当前仅域名入口。', '按 A 打开自动优选设置']},
                 'focus': 'maintenance', 'shortcut': 'auto-settings'}
        actions = [('install', '部署', ''), ('subscription', '订阅', ''), ('maintenance', '维护', ''), ('exit', '退出', '')]
        for dimensions in ((20, 52), (24, 80), (32, 120)):
            with self.subTest(dimensions=dimensions):
                ui, screen = self.ui(['q'], dimensions)
                ui.home(model, actions)
                text = '\n'.join(row[2] for row in screen.writes)
                self.assertIn('PrivateXUI', text)
                self.assertGreaterEqual(text.count('╭'), 7)
                self.assertIn('尚未启用自动优选', text)
                self.assertTrue(any('3 维护' in row[2] and row[3] == ui.selected for row in screen.writes))

    def test_auto_shortcut_opens_settings_and_never_deploys(self):
        ui, _ = self.ui(['a'])
        model = {'server': {'title': '服务器', 'badge': ('正常', 'good'), 'rows': []},
                 'deployment': {'title': '订阅', 'badge': ('已配置', 'good'), 'rows': []},
                 'notice': {'title': '待配置', 'tone': 'warning', 'lines': []}, 'shortcut': 'auto-settings'}
        self.assertEqual(ui.home(model, [('install', '部署', ''), ('exit', '退出', '')]), 'auto-settings')

    def test_form_backspace_default_and_validation(self):
        ui, _ = self.ui(['\n', '\x15', 'o', 'k', 'x', tui.curses.KEY_BACKSPACE, '\n'])
        def validate(value):
            if value != 'ok':
                raise ValueError('invalid')
            return value
        self.assertEqual(ui.ask('输入', '名称', validate, default='old'), 'ok')

    def test_form_escape_and_optional_empty(self):
        ui, _ = self.ui(['\x1b', '\n'])
        with self.assertRaises(tui.Cancelled): ui.ask('输入', '名称')
        self.assertIsNone(ui.ask('输入', '名称', allow_empty=True))

    def test_destructive_confirmation_defaults_to_cancel(self):
        ui, _ = self.ui(['\n'])
        self.assertFalse(ui.confirm('清理', ['确认删除本项目'], destructive=True))

    def test_console_restores_terminal_after_failure(self):
        ui, _ = self.ui([])
        with patch.object(tui.curses, 'def_prog_mode'), patch.object(tui.curses, 'endwin'), \
             patch.object(tui.curses, 'reset_prog_mode') as restore, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                ui.console(lambda: (_ for _ in ()).throw(RuntimeError('fail')))
        restore.assert_called_once()

    def test_dumb_and_redirected_terminal_use_fallback(self):
        with patch.dict(os.environ, {'TERM': 'dumb'}), patch.object(sys.stdin, 'isatty', return_value=True), \
             patch.object(sys.stdout, 'isatty', return_value=True):
            self.assertFalse(tui.available())
        with patch.dict(os.environ, {'TERM': 'xterm-256color'}), patch.object(sys.stdin, 'isatty', return_value=True), \
             patch.object(sys.stdout, 'isatty', return_value=False):
            self.assertFalse(tui.available())


class GuidedUITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'state.json'

    def test_dashboard_exposes_install_status_but_no_secrets(self):
        self.path.write_text(json.dumps(STATE))
        with patch.object(wizard, 'panel_status', return_value='已安装 · 运行中'):
            lines = '\n'.join(wizard.status_lines(self.path, REPORT, True))
        self.assertIn('已部署（本机记录）', lines)
        self.assertIn('6 条节点配置', lines)
        self.assertNotIn(STATE['uuid'], lines)
        self.assertNotIn(STATE['subscription_token'], lines)

    def test_auto_candidates_do_not_claim_a_static_node_count(self):
        state = dict(STATE, preferred_mode='auto', preferred=[])
        self.path.write_text(json.dumps(state))
        self.assertIsNone(wizard.subscription_count(state))
        with patch.object(wizard, 'panel_status', return_value='已安装'):
            lines = '\n'.join(wizard.status_lines(self.path, REPORT, True))
        self.assertIn('动态优选', lines)
        self.assertNotIn('3 条节点配置', lines)

    def test_legacy_domain_only_is_yellow_with_explicit_action_and_no_mutation(self):
        state = dict(STATE, preferred=[])
        original = json.dumps(state)
        self.path.write_text(original)
        with patch.object(wizard, 'panel_status', return_value='已安装 · 运行中'):
            model = wizard.dashboard_state(self.path, REPORT, True)
        self.assertEqual(model['notice']['tone'], 'warning')
        self.assertEqual(model['shortcut'], 'auto-settings')
        self.assertIn('当前仅域名入口', model['notice']['lines'][0])
        self.assertIn('自动优选', model['notice']['lines'][1])
        self.assertEqual(self.path.read_text(), original)
        serialized = json.dumps(model, ensure_ascii=False)
        self.assertNotIn('3 条节点配置', serialized)
        self.assertNotIn(STATE['subscription_token'], serialized)
        self.assertNotIn(STATE['uuid'], serialized)

    def test_stopped_panel_is_red_even_with_ready_local_deployment(self):
        self.path.write_text(json.dumps(dict(STATE, preferred_mode='auto')))
        with patch.object(wizard, 'panel_status', return_value='已安装 · 服务未运行'):
            model = wizard.dashboard_state(self.path, REPORT, True)
        self.assertEqual(model['server']['badge'][1], 'danger')
        self.assertEqual(model['notice']['tone'], 'danger')
        self.assertIn('3x-ui', model['notice']['title'])

    def test_dashboard_corrupt_state_is_red_and_preserved(self):
        self.path.write_text('bad-state')
        with patch.object(wizard, 'panel_status', return_value='已安装 · 运行中'):
            model = wizard.dashboard_state(self.path, REPORT, True)
        self.assertEqual(model['deployment']['badge'][1], 'danger')
        self.assertEqual(model['notice']['tone'], 'danger')
        self.assertEqual(self.path.read_text(), 'bad-state')

    def test_automatic_entry_shortcut_still_requires_explicit_confirmation(self):
        self.path.write_text(json.dumps(dict(STATE, preferred=[])))
        ui = Mock()
        ui.choose.return_value = 'auto'
        ui.confirm.return_value = False
        controller = wizard.GuidedUI(str(self.path), REPORT, ui)
        with patch.object(controller, 'command') as command:
            controller.settings(suggest_auto=True)
        self.assertEqual(ui.choose.call_args.kwargs['initial'], 'auto')
        ui.ask.assert_not_called()
        ui.confirm.assert_called_once()
        command.assert_not_called()

    def test_missing_and_broken_deployment_are_distinct(self):
        with patch.object(wizard, 'panel_status', return_value='未安装'):
            self.assertIn('本项目   未部署', wizard.status_lines(self.path, REPORT, False))
            self.path.write_text('invalid-json')
            self.assertIn('本项目   状态读取失败 · 请保留恢复文件', wizard.status_lines(self.path, REPORT, False))
        self.assertEqual(self.path.read_text(), 'invalid-json')

    def test_subscription_is_single_full_yaml_link_not_filtered_or_base64(self):
        self.path.write_text(json.dumps(STATE))
        out = io.StringIO()
        with contextlib.redirect_stdout(out): wizard.show_subscription(self.path)
        value = out.getvalue()
        self.assertEqual(value.count('https://'), 1)
        self.assertIn('/Private-XUI.yaml', value)
        self.assertIn('我的订阅', value)
        self.assertNotIn('?protocol=', value)
        self.assertNotIn('base64', value.lower())
        self.assertNotIn('防火墙', value)

    def test_install_defaults_to_dynamic_candidates_without_json_prompt(self):
        with patch('builtins.input', side_effect=['', 'node.example.com', 'sub.example.com', '', 'y']), \
             contextlib.redirect_stdout(io.StringIO()):
            args = wizard.install_questions(str(self.path), REPORT, False)
        self.assertEqual(args.preferred_mode, 'auto')
        self.assertIsNone(args.preferred)
        self.assertTrue(args.quiet_links)
        self.assertEqual(args.protocols, 'vless,trojan,vmess')

    def test_tui_install_collects_data_before_backend_and_cancels_cleanly(self):
        ui = Mock()
        ui.ask.side_effect = ['1.1.1.1', 'node.example.com', 'sub.example.com', 'vless', '我的订阅']
        ui.confirm.return_value = False
        controller = wizard.GuidedUI(str(self.path), REPORT, ui)
        with patch.object(manage, 'run_command') as run:
            controller.install(False)
        run.assert_not_called()
        ui.console.assert_not_called()

    def test_existing_state_blocks_repeat_install(self):
        self.path.write_text(json.dumps(STATE))
        ui = Mock()
        controller = wizard.GuidedUI(str(self.path), REPORT, ui)
        controller.install(True)
        ui.ask.assert_not_called()
        ui.console.assert_called_once()

    def test_update_does_not_ask_for_json_or_change_candidates(self):
        ui = Mock()
        ui.choose.side_effect = ['update', None]
        controller = wizard.GuidedUI(str(self.path), REPORT, ui)
        with patch.object(controller, 'command') as command:
            controller.maintenance()
        command.assert_called_once_with('update-worker', preferred=None, preferred_mode=None, subscription_name=None)
        ui.ask.assert_not_called()

    def test_settings_keeps_custom_candidates_by_default(self):
        self.path.write_text(json.dumps(STATE))
        ui = Mock()
        ui.ask.return_value = 'new name'
        ui.choose.return_value = 'keep'
        ui.confirm.return_value = True
        controller = wizard.GuidedUI(str(self.path), REPORT, ui)
        with patch.object(controller, 'command') as command: controller.settings()
        command.assert_called_once_with('update-worker', preferred=None, preferred_mode=None, subscription_name='new name')

    def test_cancellation_is_noop_and_name_cannot_inject_headers(self):
        ui = Mock()
        ui.ask.side_effect = tui.Cancelled()
        controller = wizard.GuidedUI(str(self.path), REPORT, ui)
        with patch.object(manage, 'run_command') as run:
            with self.assertRaises(tui.Cancelled): controller.install(False)
        run.assert_not_called()
        for value in ['', 'x\r\nHeader: evil', 'x' * 81]:
            with self.assertRaises(ValueError): wizard.subscription_name(value)


if __name__ == '__main__':
    unittest.main()
