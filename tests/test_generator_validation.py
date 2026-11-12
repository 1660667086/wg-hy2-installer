"""Input validation checks; no network or elevated privileges required."""
import argparse
import importlib.util
from pathlib import Path
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


if __name__ == '__main__':
    unittest.main()
