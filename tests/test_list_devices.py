"""list_devices() must never back a device up under its network address (#869).

An empty `getprop ro.serialno` over Wi-Fi used to fall back to the address, so a phone
was backed up in full a second time under <model>-<ip>_5555.

Run: python -m unittest discover -s tests
"""
import subprocess
import unittest
from unittest import mock

from android_backup import __main__ as ab

WIFI = "192.168.1.50:5555"
USB = "R5CT1234ABC"


def fake_adb(devices, serials):
    """devices: `adb devices` rows. serials: address -> getprop output, or an exception to raise."""
    def adb(*args, timeout=120):
        if args == ("devices",):
            out = "List of devices attached\n" + "".join(f"{a}\t{s}\n" for a, s in devices)
            return subprocess.CompletedProcess(args, 0, out, "")
        if args[0] == "-s" and args[2:] == ("shell", "-T", "getprop ro.serialno"):
            value = serials[args[1]]
            if isinstance(value, Exception):
                raise value
            return subprocess.CompletedProcess(args, 0 if value else 1, value, "" if value else "error: closed")
        raise AssertionError(f"unexpected adb call {args}")
    return adb


class ListDevices(unittest.TestCase):
    def run_with(self, devices, serials):
        with mock.patch.object(ab, "adb", fake_adb(devices, serials)), \
             mock.patch.object(ab, "SERIALS", set()), mock.patch.object(ab, "log"):
            return ab.list_devices()

    def test_wifi_empty_serial_is_not_ready(self):
        ready, _ = self.run_with([(WIFI, "device")], {WIFI: ""})
        self.assertEqual(ready, {})

    def test_wifi_whitespace_serial_is_not_ready(self):
        ready, _ = self.run_with([(WIFI, "device")], {WIFI: "\n"})
        self.assertEqual(ready, {})

    def test_wifi_timeout_is_not_ready(self):
        ready, _ = self.run_with([(WIFI, "device")], {WIFI: subprocess.TimeoutExpired("adb", 20)})
        self.assertEqual(ready, {})

    def test_wifi_real_serial(self):
        ready, _ = self.run_with([(WIFI, "device")], {WIFI: USB + "\n"})
        self.assertEqual(ready, {USB: WIFI})

    def test_usb_address_is_the_serial(self):
        ready, _ = self.run_with([(USB, "device")], {USB: ""})
        self.assertEqual(ready, {USB: USB})

    def test_usb_preferred_over_wifi(self):
        ready, _ = self.run_with([(WIFI, "device"), (USB, "device")], {WIFI: USB, USB: USB})
        self.assertEqual(ready, {USB: USB})

    def test_empty_wifi_serial_next_to_usb_adds_no_duplicate(self):
        ready, _ = self.run_with([(WIFI, "device"), (USB, "device")], {WIFI: "", USB: USB})
        self.assertEqual(ready, {USB: USB})

    def test_unauthorized(self):
        ready, unauthorized = self.run_with([(WIFI, "unauthorized")], {})
        self.assertEqual((ready, unauthorized), ({}, [WIFI]))


if __name__ == "__main__":
    unittest.main()
