"""Test the bundled menu using a real terminal and a piped script."""
import errno
import importlib.util
import os
from pathlib import Path
import pty
import select
import signal
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('bundle_builder', ROOT / 'build_single_file.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def wait_exit(pid, timeout=5):
    """The pty reports EOF slightly before the child can be reaped, so poll."""
    deadline = time.monotonic() + timeout
    while True:
        ended, status = os.waitpid(pid, os.WNOHANG)
        if ended == pid or time.monotonic() > deadline:
            return ended, status
        time.sleep(0.02)


class BundleTest(unittest.TestCase):
    def test_committed_file_matches_builder(self):
        self.assertTrue(builder.is_current((ROOT / 'install.sh').read_text()),
                        'install.sh is out of date; run python3 build_single_file.py')

    def test_check_ignores_compression_bytes_but_not_content(self):
        # Another zlib (e.g. zlib-ng) compresses differently; that is not staleness.
        self.assertTrue(builder.is_current(builder.render(builder.archive(), level=1)))
        original = builder.contents
        with patch.object(builder, 'contents',
                          side_effect=lambda name: original(name) + (b'# edit\n' if name == 'menu.sh' else b'')):
            stale = builder.build()
        self.assertFalse(builder.is_current(stale))
        self.assertFalse(builder.is_current(builder.build().replace('umask 077', 'umask 022', 1)))
        self.assertFalse(builder.is_current('#!/bin/sh\n'))

    def test_piped_installer_opens_menu_and_exits(self):
        original = builder.contents
        def patched(name):
            raw = original(name)
            if name == 'menu.sh':
                raw = raw.replace(b'[[ $EUID -eq 0 ]]', b'true', 1)
                raw = raw.replace(b'[[ $(uname -s) == Linux ]]', b'true', 1)
            return raw

        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / 'install.sh'
            with patch.object(builder, 'contents', side_effect=patched):
                script.write_text(builder.build())
            pid, fd = pty.fork()
            if pid == 0:
                os.execv('/bin/bash', ['/bin/bash', '-c', 'bash -s < "$1"', 'bundle-test', str(script)])
            output = bytearray()
            try:
                os.write(fd, b'0\n')
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    if not select.select([fd], [], [], 0.1)[0]:
                        continue
                    try:
                        output.extend(os.read(fd, 65536))
                    except OSError as exc:
                        if exc.errno == errno.EIO:
                            break
                        raise
                ended, status = wait_exit(pid)
                self.assertEqual(ended, pid, 'menu did not exit')
                self.assertEqual(os.waitstatus_to_exitcode(status), 0)
                self.assertIn('WG + HY2 安装管理', output.decode('utf-8', 'replace'))
                self.assertIn('已退出', output.decode('utf-8', 'replace'))
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
