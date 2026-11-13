"""Exercise the interactive menu through a real pseudo-terminal without installs."""
import errno
import os
from pathlib import Path
import pty
import select
import signal
import time
import unittest


class MenuTest(unittest.TestCase):
    def test_single_flow_accepts_ports_and_returns_after_download_failure(self):
        source = (Path(__file__).resolve().parents[1] / 'menu.sh').read_text()
        # Remove platform/root requirements only in this in-memory test copy;
        # a failing curl function ensures no network or installer is invoked.
        source = source.replace('[[ $EUID -eq 0 ]]', 'true', 1)
        source = source.replace('[[ $(uname -s) == Linux ]]', 'true', 1)
        source = 'curl() { return 1; }\n' + source
        pid, fd = pty.fork()
        if pid == 0:
            os.execv('/bin/bash', ['/bin/bash', '-c', source])
        output = b''
        try:
            for answer in ('1', '8.8.8.8', '70000', '444', '51821', 'n', '0'):
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
            ended, status = os.waitpid(pid, os.WNOHANG)
            self.assertEqual(ended, pid, 'menu did not exit')
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
            result = output.decode('utf-8', 'replace')
            for marker in ('请输入 1 到 65535', '允许 UDP 444', '下载失败', '已退出'):
                self.assertIn(marker, result)
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


if __name__ == '__main__':
    unittest.main()
