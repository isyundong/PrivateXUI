import argparse
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import environment as env
import manage as m
import wizard

REPORT = {'system': 'Ubuntu 24.04 LTS', 'architecture': 'x86_64', 'python': '3.12.0',
          'ipv4': '1.1.1.1', 'issues': [], 'supported': True, 'ip_checked': True}


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        patches = [patch.object(env.platform, 'system', return_value='Linux'),
                   patch.object(env.platform, 'machine', return_value='x86_64'),
                   patch.object(env, 'os_release', return_value={'ID': 'ubuntu', 'VERSION_ID': '24.04', 'PRETTY_NAME': 'Ubuntu 24.04'}),
                   patch.object(env.os, 'geteuid', return_value=0, create=True),
                   patch.object(env.Path, 'is_dir', return_value=True),
                   patch.object(env.shutil, 'which', return_value='/usr/bin/systemctl'),
                   patch.object(env, 'public_ipv4', return_value='1.1.1.1')]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_supported_environment_reports_public_ip(self):
        report = env.inspect_server()
        self.assertTrue(report['supported'])
        self.assertEqual(report['ipv4'], '1.1.1.1')

    def test_desktop_fails_with_ssh_guidance_before_network(self):
        env.platform.system.return_value = 'Darwin'
        report = env.inspect_server()
        self.assertFalse(report['supported'])
        self.assertIn('ssh root@', '\n'.join(report['issues']))
        env.public_ipv4.assert_not_called()

    def test_unsupported_os_arch_systemd_and_permission_are_explained(self):
        env.os_release.return_value = {'ID': 'ubuntu', 'VERSION_ID': '18.04'}
        env.platform.machine.return_value = 'armv7l'
        env.Path.is_dir.return_value = False
        env.os.geteuid.return_value = 1000
        report = env.inspect_server()
        self.assertEqual(len(report['issues']), 4)
        env.public_ipv4.assert_not_called()

    def test_ip_detection_failure_allows_manual_input(self):
        env.public_ipv4.return_value = None
        report = env.inspect_server()
        self.assertTrue(report['supported'])
        out = io.StringIO()
        with contextlib.redirect_stdout(out): env.print_report(report)
        self.assertIn('手动输入', out.getvalue())

    def test_ip_validation_rejects_private_ipv6_and_placeholder(self):
        for address in ('192.168.1.2', '127.0.0.1', '203.0.113.10', '2001:db8::1', 'https://1.1.1.1'):
            with self.assertRaises(ValueError): env.validate_ipv4(address)
        self.assertEqual(env.validate_ipv4('1.1.1.1'), '1.1.1.1')


class WizardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = str(Path(self.tmp.name) / 'state.json')
        quiet = contextlib.redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def test_wizard_reprompts_invalid_ip_domain_and_protocol(self):
        answers = ['192.168.1.1', '', 'node.example.com', 'node.example.com', 'sub.example.com', 'bad', '1,2', '', 'y']
        with patch('builtins.input', side_effect=answers):
            args = wizard.install_questions(self.state, REPORT, installed=False)
        self.assertTrue(args.fresh)
        self.assertEqual(args.ipv4, '1.1.1.1')
        self.assertEqual(args.protocols, 'vless,trojan')
        self.assertEqual(args.sub_domain, 'sub.example.com')

    def test_cancel_makes_no_deployment_call(self):
        with patch('builtins.input', side_effect=['', 'node.example.com', 'sub.example.com', '', '', 'n']), patch.object(m, 'run_command') as run:
            self.assertIsNone(wizard.install_questions(self.state, REPORT, installed=True))
        run.assert_not_called()

    def test_menu_existing_panel_dispatches_install_then_exit(self):
        answers = ['1', '', 'node.example.com', 'sub.example.com', '1', '', 'y', '0']
        with patch.object(sys.stdin, 'isatty', return_value=True), patch.object(wizard, 'inspect_server', return_value=REPORT), patch.object(m.xui, 'is_xui_installed', return_value=True), patch('builtins.input', side_effect=answers), patch.object(m, 'run_command') as run:
            wizard.menu(self.state)
        run.assert_called_once()
        self.assertFalse(run.call_args.args[0].fresh)

    def test_unsupported_environment_never_enters_menu_or_asks_credentials(self):
        invalid = dict(REPORT, supported=False, issues=['需要 Linux VPS'])
        with patch.object(sys.stdin, 'isatty', return_value=True), patch.object(wizard, 'inspect_server', return_value=invalid), patch('builtins.input') as read, patch.object(m, 'cf_client') as cf:
            with self.assertRaises(ValueError): wizard.menu(self.state)
        read.assert_not_called()
        cf.assert_not_called()

    def test_plain_cli_install_checks_environment_before_cloud_access(self):
        invalid = dict(REPORT, supported=False, issues=['unsupported'])
        with patch.object(m, 'inspect_server', return_value=invalid), patch.object(m, 'cf_client') as cf:
            with self.assertRaises(SystemExit): m.main(['install'])
        cf.assert_not_called()

    def test_no_arguments_open_menu_and_show_remains_read_only(self):
        with patch.object(wizard, 'menu') as menu:
            m.main(['--state', self.state])
        menu.assert_called_once_with(self.state)
        with patch.object(m, 'load', return_value={}), patch.object(m, 'print_links'), patch.object(m, 'inspect_server') as inspect, patch.object(m, 'cf_client') as cf:
            m.main(['--state', self.state, 'show'])
        inspect.assert_not_called()
        cf.assert_not_called()


class DistributionTests(unittest.TestCase):
    def test_zipapp_contains_and_reads_worker_without_source_directory(self):
        subprocess.run([sys.executable, str(ROOT / 'tools/build.py'), '--check'], check=True, capture_output=True)
        program = ROOT / 'dist/private-xui.pyz'
        code = 'import sys; sys.path.insert(0, sys.argv[1]); import manage; print(manage.worker_source()); print(manage.parser().parse_args([]).command)'
        with tempfile.TemporaryDirectory() as cwd:
            result = subprocess.run([sys.executable, '-c', code, str(program)], cwd=cwd, check=True, text=True, capture_output=True)
        self.assertIn("|| 'clash'", result.stdout)
        self.assertTrue(result.stdout.strip().endswith('None'))

    def test_bootstrap_on_desktop_stops_before_downloading_or_installing(self):
        with tempfile.TemporaryDirectory() as directory:
            stub = Path(directory) / 'uname'
            stub.write_text('#!/bin/sh\necho Darwin\n')
            stub.chmod(0o755)
            # PATH has no curl, mkdir, id, or Python: none should be invoked.
            result = subprocess.run(['/bin/bash', str(ROOT / 'install.sh')], env={**os.environ, 'PATH': directory}, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('ssh root@', result.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
