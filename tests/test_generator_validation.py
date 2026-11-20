"""Input validation checks; no network or elevated privileges required."""
import argparse
import importlib.util
from pathlib import Path
import subprocess
import sys
import unittest

MODULE = Path(__file__).resolve().parents[1] / 'wg_hy2_generate.py'
spec = importlib.util.spec_from_file_location('wg_hy2_generate', MODULE)
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)


class AddressValidationTest(unittest.TestCase):
    def test_accepts_public_unicast_ipv4(self):
        self.assertEqual(generator.public_ipv4('8.8.8.8'), '8.8.8.8')

    def test_rejects_nonpublic_multicast_and_malformed(self):
        values = ('224.0.0.1', '0.0.0.0', '127.0.0.1', '169.254.169.254',
                  '10.0.0.1', '100.64.0.1', '192.0.2.1', '240.0.0.1',
                  '255.255.255.255', '::1', '8.8.8.8;echo bad')
        for address in values:
            with self.subTest(address=address), self.assertRaises(argparse.ArgumentTypeError):
                generator.public_ipv4(address)

    def test_port_boundaries(self):
        for value in ('1', '443', '65535'):
            self.assertEqual(generator.port_number(value), int(value))
        for value in ('0', '65536', '-1', 'a'):
            with self.subTest(port=value), self.assertRaises(argparse.ArgumentTypeError):
                generator.port_number(value)

    def test_check_only_validates_without_sing_box(self):
        def check(*args):
            return subprocess.run([sys.executable, str(MODULE), *args], capture_output=True, text=True)
        ok = check('--mode', 'single', '--entry-ip', '8.8.8.8', '--check-only')
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(ok.stdout, '')
        for args in (('--mode', 'single', '--entry-ip', '10.0.0.1'),
                     ('--mode', 'single', '--entry-ip', '8.8.8.8', '--hy2-port', '443', '--wg-port', '443'),
                     ('--mode', 'dual', '--entry-ip', '8.8.8.8', '--exit-ip', '8.8.8.8'),
                     ('--mode', 'dual', '--entry-ip', '8.8.8.8')):
            with self.subTest(args=args):
                self.assertNotEqual(check(*args, '--check-only').returncode, 0)
        # Without --check-only, generation still needs a binary and an output directory.
        self.assertEqual(check('--mode', 'single', '--entry-ip', '8.8.8.8').returncode, 2)


if __name__ == '__main__':
    unittest.main()
