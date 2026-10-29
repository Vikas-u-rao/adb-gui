#!/usr/bin/env python3
"""Unit tests for adb_core parsing helpers. Run with: python -m unittest -v"""
import unittest

from adb_core import (
    Adb, human_size, normalize_remote, parse_devices, parse_ls, parse_packages,
    remote_join, remote_parent,
)

MODERN_LS = """total 24
drwxrwx--x 4 root sdcard_rw 4096 2024-05-01 10:22 Android
-rw-rw---- 1 u0_a123 ext_data_rw 12345 2024-05-01 10:23 my file.txt
lrwxrwxrwx 1 root root 21 2024-05-01 10:22 sdcard -> /storage/self/primary
drwxrwx--x 4 root sdcard_rw 4096 2024-05-01 10:22 .
drwxrwx--x 4 root sdcard_rw 4096 2024-05-01 10:22 ..
"""

LEGACY_LS = """drwxr-xr-x root root 2013-01-01 12:34 legacy
-rw-r--r-- root root 42 Jan  1 2013 old.txt
-rw-r--r-- 1000 1000 4096 2024-01-02 03:04 numeric.txt
"""

DEVICES = """List of devices attached
R58M123ABC\tdevice usb:1-1 product:beyond1q model:SM_G973U1 device:beyond1 transport_id:1
192.168.1.5:5555\tdevice product:panther model:Pixel_7 device:panther transport_id:2
emulator-5554\toffline
ABC123\tunauthorized usb:1-2 transport_id:3
XYZ999\tno permissions (user in plugdev group; are your udev rules wrong?); see [http://developer.android.com]
* daemon not running; starting now at tcp:5037
"""

PACKAGES = """package:/data/app/~~abc==/com.example.app-xyz==/base.apk=com.example.app
package:/system/app/Settings/Settings.apk=com.android.settings
package:/system_ext/app/Foo/Foo.apk=com.android.foo
warning: something else
"""


class ParseLsTests(unittest.TestCase):
    def test_modern_listing(self):
        entries = parse_ls(MODERN_LS)
        names = [entry.name for entry in entries]
        self.assertEqual(names, ["Android", "sdcard", "my file.txt"])
        self.assertTrue(entries[0].is_dir)
        self.assertFalse(entries[0].is_link)
        self.assertEqual(entries[1].link_target, "/storage/self/primary")
        self.assertTrue(entries[1].is_link)
        self.assertEqual(entries[2].size, 12345)
        self.assertEqual(entries[2].modified, "2024-05-01 10:23")

    def test_legacy_and_numeric_owners(self):
        entries = parse_ls(LEGACY_LS)
        names = [entry.name for entry in entries]
        self.assertIn("legacy", names)
        self.assertIn("old.txt", names)
        self.assertIn("numeric.txt", names)
        numeric = next(entry for entry in entries if entry.name == "numeric.txt")
        self.assertEqual(numeric.size, 4096)

    def test_junk_lines_ignored(self):
        self.assertEqual(parse_ls("total 0\nnot a listing\n"), [])


class ParseDevicesTests(unittest.TestCase):
    def test_devices(self):
        devices = parse_devices(DEVICES)
        self.assertEqual(len(devices), 5)
        self.assertEqual(devices[0].serial, "R58M123ABC")
        self.assertEqual(devices[0].state, "device")
        self.assertEqual(devices[0].model, "SM_G973U1")
        self.assertEqual(devices[0].display_name, "SM G973U1")
        self.assertTrue(devices[0].usable)
        self.assertEqual(devices[1].serial, "192.168.1.5:5555")
        self.assertEqual(devices[2].state, "offline")
        self.assertEqual(devices[3].state, "unauthorized")
        self.assertFalse(devices[3].usable)
        self.assertEqual(devices[4].state, "no permissions")
        self.assertFalse(devices[4].usable)

    def test_empty(self):
        self.assertEqual(parse_devices("List of devices attached\n"), [])
        self.assertEqual(parse_devices(""), [])


class ParsePackagesTests(unittest.TestCase):
    def test_packages(self):
        packages = parse_packages(PACKAGES)
        self.assertEqual(len(packages), 3)
        self.assertEqual(packages[0].name, "com.android.foo")
        self.assertTrue(packages[0].system)
        self.assertEqual(packages[1].name, "com.android.settings")
        self.assertTrue(packages[1].system)
        self.assertEqual(packages[2].name, "com.example.app")
        self.assertFalse(packages[2].system)
        self.assertTrue(packages[2].apk.endswith("base.apk"))


class PathTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_remote("/sdcard/DCIM/"), "/sdcard/DCIM")
        self.assertEqual(normalize_remote("sdcard"), "/sdcard")
        self.assertEqual(normalize_remote("~"), "/sdcard")
        self.assertEqual(normalize_remote("~/Download"), "/sdcard/Download")
        self.assertEqual(normalize_remote("//sdcard//DCIM//"), "/sdcard/DCIM")
        self.assertEqual(normalize_remote(""), "/")
        self.assertEqual(normalize_remote("/"), "/")

    def test_join_and_parent(self):
        self.assertEqual(remote_join("/", "sdcard"), "/sdcard")
        self.assertEqual(remote_join("/sdcard", "a b"), "/sdcard/a b")
        self.assertEqual(remote_join("/sdcard/DCIM", "Camera"), "/sdcard/DCIM/Camera")
        self.assertEqual(remote_parent("/"), "/")
        self.assertEqual(remote_parent("/sdcard"), "/")
        self.assertEqual(remote_parent("/sdcard/DCIM/Camera"), "/sdcard/DCIM")

    def test_human_size(self):
        self.assertEqual(human_size(0), "0 B")
        self.assertEqual(human_size(1023), "1023 B")
        self.assertEqual(human_size(1024), "1.0 KB")
        self.assertEqual(human_size(1536), "1.5 KB")
        self.assertEqual(human_size(1024 ** 2), "1.0 MB")


class AdbDisplayTests(unittest.TestCase):
    def test_pairing_code_is_masked(self):
        shown = Adb("adb").display(["pair", "192.168.1.5:37099", "123456"])
        self.assertIn("******", shown)
        self.assertNotIn("123456", shown)
        self.assertIn("192.168.1.5:37099", shown)

    def test_serial_is_shown(self):
        shown = Adb("adb").display(["shell", "ls -la /sdcard/"], serial="ABC123")
        self.assertEqual(shown, "adb -s ABC123 shell 'ls -la /sdcard/'")


if __name__ == "__main__":
    unittest.main()
