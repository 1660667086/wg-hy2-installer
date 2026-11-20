"""Run the shell installers end to end against fake ssh, scp and sing-box.

Nothing here touches systemd, the firewall, /root or the network: the system
installer is a recording stub, remote commands run locally, and paths under
/root are redirected into a temporary directory in each test copy.
"""
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

import shell_fakes as fakes

sys.path.insert(0, str(fakes.ROOT))
import import_relay_token  # noqa: E402

# The "remote" side runs on this machine, which need not be Linux or root.
REMOTE_HOST_CHECKS = (('[ "$(id -u)" -eq 0 ]', 'true'), ('[ "$(uname -s)" = Linux ]', 'true'))


class ShellFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='wg-hy2-shell-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = fakes.fake_bin(self.root / 'bin')
        self.log = self.root / 'calls.log'

    def run_script(self, script, *args, env):
        return subprocess.run(['bash', str(script), *args], env=env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)

    def calls(self, kind):
        return [line[1:] for line in fakes.log_lines(self.log) if line[0] == kind]

    def assert_remote_stages_removed(self, prefix):
        stages = set()
        for _target, command in self.calls('ssh'):
            stages.update(re.findall(r'/tmp/' + re.escape(prefix) + r'\.[A-Za-z0-9]{8}', command))
        self.assertTrue(stages, 'no remote stage directory was used')
        for stage in stages:
            self.assertFalse(Path(stage).exists(), stage)


class DeployDualTest(ShellFlowTest):
    def setUp(self):
        super().setUp()
        project = fakes.stub_tools(self.root / 'project')
        self.script = fakes.patched_script('deploy_dual.sh', project, REMOTE_HOST_CHECKS)
        self.client = self.root / 'client.json'

    def deploy(self, *extra, **env):
        return self.run_script(self.script, '--relay-host', '8.8.4.4', '--landing-host', '9.9.9.9',
                               '--client-output', str(self.client), *extra,
                               env=fakes.environment(self.bin, self.log, FAKE_EXPECT_FILE=self.client, **env))

    def test_enable_saves_client_first_then_installs_landing_and_relay(self):
        # --enable leaves the flag array empty, which used to abort on macOS bash 3.2.
        result = self.deploy('--enable')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.client.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(self.client.read_text())['outbounds'][0]['server'], '8.8.4.4')
        installs = self.calls('install')
        self.assertEqual([(row[0], row[2]) for row in installs],
                         [('landing', 'client-saved'), ('relay', 'client-saved')])
        self.assertTrue(all('--no-enable' not in row[1] for row in installs))
        preflights = [row[0] for row in self.calls('installer') if row[0].startswith('--preflight')]
        self.assertEqual(preflights, ['--preflight --name landing --wg-port 51820',
                                      '--preflight --name relay --hy2-port 443'])
        self.assert_remote_stages_removed('wg-hy2-deploy')

    def test_default_run_disables_autostart(self):
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all('--no-enable' in row[1] for row in self.calls('install')))

    def test_existing_client_output_is_refused_before_contacting_servers(self):
        self.client.write_text('keep')
        result = self.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('refusing to overwrite', result.stderr)
        self.assertEqual(self.client.read_text(), 'keep')
        self.assertEqual(self.calls('ssh'), [])

    def test_failed_relay_preflight_installs_nothing(self):
        result = self.deploy(FAKE_PREFLIGHT_FAIL='relay')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('failed its pre-install check', result.stderr)
        self.assertEqual(self.calls('install'), [])
        self.assertFalse(self.client.exists())
        self.assert_remote_stages_removed('wg-hy2-deploy')

    def test_unreachable_server_explains_ssh_setup(self):
        result = self.deploy(FAKE_SSH_FAIL=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('verify the host key', result.stderr)
        self.assertFalse(self.client.exists())

    def test_non_root_user_runs_privileged_steps_with_sudo(self):
        result = self.deploy('--ssh-user', 'ubuntu')
        self.assertEqual(result.returncode, 0, result.stderr)
        remote = [command for target, command in self.calls('ssh') if 'bash -s' in command]
        self.assertEqual(len(remote), 4)
        self.assertTrue(all(command.startswith('sudo -n bash -s') for command in remote))
        self.assertTrue(all(target.startswith('ubuntu@') for target, _ in self.calls('ssh')))


class ServerDualInstallTest(ShellFlowTest):
    def setUp(self):
        super().setUp()
        self.raw = fakes.stub_tools(self.root / 'raw')
        self.recovery_root = self.root / 'recovery'
        self.recovery_root.mkdir()
        self.script = fakes.patched_script('server_dual_install.sh', self.root, (
            ('[[ $EUID -eq 0 ]]', 'true'),
            ('/root/wg-hy2-recovery.', f'{self.recovery_root}/wg-hy2-recovery.'),
            *REMOTE_HOST_CHECKS))
        self.client = self.root / 'client.json'

    def install(self, *extra, **env):
        return self.run_script(self.script, '--relay-ip', '8.8.4.4', '--landing-host', '9.9.9.9',
                               '--landing-ip', '9.9.9.9', '--client-output', str(self.client), *extra,
                               env=fakes.environment(self.bin, self.log, self.raw,
                                                     FAKE_EXPECT_FILE=self.client, **env))

    def assert_nothing_generated(self, result):
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.client.exists())
        self.assertEqual(list(self.recovery_root.iterdir()), [])
        self.assertEqual(self.calls('install'), [])

    def test_ssh_problem_is_reported_before_credentials_exist(self):
        result = self.install(FAKE_SSH_FAIL=1)
        self.assert_nothing_generated(result)
        self.assertIn('核对并接受主机指纹', result.stderr)

    def test_landing_preflight_failure_generates_nothing(self):
        result = self.install(FAKE_PREFLIGHT_FAIL='landing')
        self.assert_nothing_generated(result)
        self.assertIn('落地机安装前检查未通过', result.stderr)
        self.assert_remote_stages_removed('wg-hy2-server-dual')

    def test_relay_preflight_failure_generates_nothing(self):
        result = self.install(FAKE_PREFLIGHT_FAIL='relay')
        self.assert_nothing_generated(result)
        self.assertIn('中转机安装前检查未通过', result.stderr)
        self.assertEqual(self.calls('ssh'), [])

    def test_invalid_address_is_rejected_first(self):
        result = self.run_script(self.script, '--relay-ip', '10.0.0.1', '--landing-host', '9.9.9.9',
                                 '--landing-ip', '9.9.9.9', '--client-output', str(self.client),
                                 env=fakes.environment(self.bin, self.log, self.raw))
        self.assert_nothing_generated(result)
        self.assertEqual(self.calls('installer'), [])

    def test_enable_saves_client_then_installs_landing_and_relay(self):
        result = self.install('--enable')
        self.assertEqual(result.returncode, 0, result.stderr)
        installs = self.calls('install')
        self.assertEqual([(row[0], row[2]) for row in installs],
                         [('landing', 'client-saved'), ('relay', 'client-saved')])
        self.assertTrue(all('--no-enable' not in row[1] for row in installs))
        recovery = list(self.recovery_root.iterdir())
        self.assertEqual(len(recovery), 1)
        self.assertTrue((recovery[0] / 'bundle/landing/server.json').is_file())
        self.assertEqual(self.client.stat().st_mode & 0o777, 0o600)
        self.assert_remote_stages_removed('wg-hy2-server-dual')

    def test_non_root_landing_user_uses_sudo(self):
        result = self.install('--landing-user', 'ubuntu')
        self.assertEqual(result.returncode, 0, result.stderr)
        remote = [command for _target, command in self.calls('ssh') if 'bash -s' in command]
        self.assertEqual(len(remote), 2)
        self.assertTrue(all(command.startswith('sudo -n bash -s') for command in remote))


class ServerRoleInstallTest(ShellFlowTest):
    """server_single_install.sh and server_landing_prepare.sh share one flow."""

    def setUp(self):
        super().setUp()
        self.tools = fakes.stub_tools(self.root / 'tools')
        self.state_root = self.root / 'state'
        self.state_root.mkdir()

    def script(self, name, state_path):
        return fakes.patched_script(name, self.tools, (
            ('[[ $EUID -eq 0 ]]', 'true'), ('[[ $(uname -s) == Linux ]]', 'true'),
            (state_path, f'{self.state_root}/{Path(state_path).name}')))

    def single(self, *args, **env):
        script = self.script('server_single_install.sh', '/root/wg-hy2-')
        return self.run_script(script, *args, env=fakes.environment(self.bin, self.log, **env))

    def test_invalid_address_changes_nothing(self):
        result = self.single('--server-ip', '10.1.2.3')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('参数有误', result.stderr)
        self.assertEqual(list(self.state_root.iterdir()), [])
        self.assertEqual(self.calls('installer'), [])

    def test_failed_preflight_leaves_no_state_directory(self):
        result = self.single('--server-ip', '8.8.8.8', FAKE_PREFLIGHT_FAIL='single')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('安装前检查未通过', result.stderr)
        self.assertEqual(list(self.state_root.iterdir()), [])
        self.assertEqual(self.calls('install'), [])

    def test_leftover_of_failed_attempt_is_moved_aside_on_retry(self):
        state = self.state_root / 'wg-hy2-single'
        (state / 'bundle').mkdir(parents=True)
        (state / 'bundle/marker').write_text('earlier attempt')
        result = self.single('--server-ip', '8.8.8.8', '--hy2-port', '8443')
        self.assertEqual(result.returncode, 0, result.stderr)
        moved = [p for p in self.state_root.iterdir() if p.name.startswith('wg-hy2-single.old-')]
        self.assertEqual(len(moved), 1)
        self.assertEqual((moved[0] / 'bundle/marker').read_text(), 'earlier attempt')
        manifest = json.loads((state / 'bundle/single/manifest.json').read_text())
        self.assertEqual((manifest['hy2_port'], manifest['wg_port']), (8443, 51820))
        install, = self.calls('install')
        self.assertEqual(install[0], 'single')
        self.assertIn('--no-enable', install[1])

    def test_landing_handoff_token_imports_on_relay(self):
        script = self.script('server_landing_prepare.sh', '/root/wg-hy2-landing-handoff')
        result = self.run_script(script, '--relay-ip', '8.8.4.4', '--landing-ip', '9.9.9.9', '--enable',
                                 env=fakes.environment(self.bin, self.log))
        self.assertEqual(result.returncode, 0, result.stderr)
        handoff = self.state_root / 'wg-hy2-landing-handoff'
        self.assertTrue((handoff / 'installed').is_file())
        install, = self.calls('install')
        self.assertEqual(install[0], 'landing')
        self.assertNotIn('--no-enable', install[1])
        relay = self.root / 'relay-import'
        relay.mkdir()
        import_relay_token.unpack(handoff / 'relay-token.txt', relay)
        self.assertEqual(json.loads((relay / 'client.json').read_text())['outbounds'][0]['server'], '8.8.4.4')


if __name__ == '__main__':
    unittest.main()
