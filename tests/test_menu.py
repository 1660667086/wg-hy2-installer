"""Exercise the interactive menu through a real pseudo-terminal without installs."""
import errno
import os
from pathlib import Path
import pty
import select
import signal
import time
import unittest

# Stands in for the downloaded installer: it only prints the arguments it got.
ECHO_INSTALLER_CURL = r'''curl() {
    local out=
    while (( $# )); do
        if [[ $1 == --output ]]; then out=$2; shift; fi
        shift
    done
    printf '%s\n' 'printf "ARGS:"; printf "%s|" "$@"; printf "\n"' > "$out"
}
'''


def wait_exit(pid, timeout=5):
    """The pty reports EOF slightly before the child can be reaped, so poll."""
    deadline = time.monotonic() + timeout
    while True:
        ended, status = os.waitpid(pid, os.WNOHANG)
        if ended == pid or time.monotonic() > deadline:
            return ended, status
        time.sleep(0.02)


def run_menu(answers, prelude):
    source = (Path(__file__).resolve().parents[1] / 'menu.sh').read_text()
    # Remove platform/root requirements only in this in-memory test copy;
    # the curl function in the prelude decides what "downloading" does.
    source = source.replace('[[ $EUID -eq 0 ]]', 'true', 1)
    source = source.replace('[[ $(uname -s) == Linux ]]', 'true', 1)
    pid, fd = pty.fork()
    if pid == 0:
        os.execv('/bin/bash', ['/bin/bash', '-c', prelude + source])
    output = b''
    try:
        for answer in answers:
            os.write(fd, (answer + '\n').encode())
            time.sleep(0.03)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not select.select([fd], [], [], 0.1)[0]:
                continue
            try:
                output += os.read(fd, 65536)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    break
                raise
        ended, status = wait_exit(pid)
        return ended == pid, (os.waitstatus_to_exitcode(status) if ended == pid else None), \
            output.decode('utf-8', 'replace')
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        os.close(fd)


class MenuTest(unittest.TestCase):
    def test_single_flow_accepts_ports_and_returns_after_download_failure(self):
        ended, code, result = run_menu(('1', '8.8.8.8', '70000', '444', '51821', 'n', '0'),
                                       'curl() { return 1; }\n')
        self.assertTrue(ended, 'menu did not exit')
        self.assertEqual(code, 0)
        for marker in ('请输入 1 到 65535', '允许 UDP 444', '下载失败', '已退出'):
            self.assertIn(marker, result)

    def test_pasted_answers_are_trimmed(self):
        ended, code, result = run_menu(('1', '   8.8.8.8   ', ' 444 ', '', ' y ', '0'),
                                       ECHO_INSTALLER_CURL)
        self.assertTrue(ended, 'menu did not exit')
        self.assertEqual(code, 0)
        self.assertIn('ARGS:--server-ip|8.8.8.8|--hy2-port|444|--wg-port|51820|--enable|', result)

    def test_import_accepts_end_marker_with_spaces(self):
        ended, code, result = run_menu(('5', 'n', 'dG9rZW4=', '  END  ', '0'), ECHO_INSTALLER_CURL)
        self.assertTrue(ended, 'menu did not exit')
        self.assertEqual(code, 0)
        self.assertRegex(result, r'ARGS:--token-file\|/tmp/wg-hy2-import\.\w+/token\|\r?\n')
        self.assertNotIn('输入中断', result)


if __name__ == '__main__':
    unittest.main()
