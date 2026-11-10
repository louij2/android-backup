"""Light mode on mobile data, and the USB rescan.

Run: python -m unittest discover -s tests
"""
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from android_backup import __main__ as ab

WIFI = "100.64.0.5:5555"


def wifi_status(out):
    return mock.patch.object(ab, "shell", return_value=subprocess.CompletedProcess([], 0, out, ""))


class OnMobileData(unittest.TestCase):
    def test_usb_is_never_mobile(self):
        with mock.patch.object(ab, "shell", side_effect=AssertionError("no device call over USB")):
            self.assertFalse(ab.on_mobile_data("R5CT1234ABC"))

    def test_wifi_connected(self):
        with wifi_status('Wifi is enabled\nWifi is connected to "home"\n'):
            self.assertFalse(ab.on_mobile_data(WIFI))

    def test_older_android_wording(self):
        with wifi_status("mNetworkInfo [type: WIFI[], state: CONNECTED/CONNECTED, reason: (unspecified)\n"):
            self.assertFalse(ab.on_mobile_data(WIFI))

    def test_wifi_off_means_mobile(self):
        with wifi_status("Wifi is disabled\n"):
            self.assertTrue(ab.on_mobile_data(WIFI))

    def test_failed_check_counts_as_mobile(self):
        with mock.patch.object(ab, "shell", side_effect=subprocess.TimeoutExpired("adb", 60)):
            self.assertTrue(ab.on_mobile_data(WIFI))


class Limit(unittest.TestCase):
    def test_smallest_first_and_rest_deferred(self):
        files = [(300_000_000, 1, "Movies/a.mp4"), (1_000, 1, "Documents/b.txt"), (250_000_000, 1, "DCIM/c.jpg")]
        pulled = []
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(ab, "storage_roots", return_value=[("/sdcard/", "files")]), \
                mock.patch.object(ab, "list_files", return_value=files), \
                mock.patch.object(ab, "pull", side_effect=lambda a, r, dest, size=0: pulled.append(r)), \
                mock.patch.object(ab, "update_state"):
            n, copied, failed, done, deferred = ab.sync_files(WIFI, "S", Path(d), open("/dev/null", "wb"),
                                                               limit=500_000_000)
        self.assertEqual(pulled, ["/sdcard/Documents/b.txt", "/sdcard/DCIM/c.jpg"])
        self.assertEqual(deferred, 300_000_000)


class Due(unittest.TestCase):
    def state(self, **s):
        return mock.patch.object(ab, "load_state", return_value={"S": s})

    def test_light_run_catches_up_on_wifi(self):
        now = time.time()
        with self.state(last_success=now - 7200, last_attempt=now - 7200, light=True), \
                mock.patch.object(ab, "on_mobile_data", return_value=False):
            self.assertTrue(ab.due("S", WIFI))

    def test_light_run_waits_while_still_on_mobile(self):
        now = time.time()
        with self.state(last_success=now - 7200, last_attempt=now - 7200, light=True), \
                mock.patch.object(ab, "on_mobile_data", return_value=True):
            self.assertFalse(ab.due("S", WIFI))

    def test_full_run_waits_for_the_interval(self):
        now = time.time()
        with self.state(last_success=now - 7200, last_attempt=now - 7200, light=False), \
                mock.patch.object(ab, "on_mobile_data", side_effect=AssertionError("no check needed")):
            self.assertFalse(ab.due("S", WIFI))


if __name__ == "__main__":
    unittest.main()
