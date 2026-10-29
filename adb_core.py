#!/usr/bin/env python3
"""Low-level helpers for the ADB GUI: adb process wrapper and output parsing.

Nothing in this module imports Qt, so it can be unit tested and reused.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# --------------------------------------------------------------------------
# adb executable discovery
# --------------------------------------------------------------------------

def _adb_candidates():
    local = os.environ.get("LOCALAPPDATA", "")
    profile = os.environ.get("USERPROFILE", "")
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    paths = [r"C:\platform-tools\adb.exe"]
    if local:
        paths.append(os.path.join(local, r"Android\Sdk\platform-tools\adb.exe"))
    if profile:
        paths.append(os.path.join(profile, r"AppData\Local\Android\Sdk\platform-tools\adb.exe"))
        paths.append(os.path.join(profile, r"platform-tools\adb.exe"))
    paths.append(os.path.join(program_files, r"Android\platform-tools\adb.exe"))
    yield from paths


def find_adb(saved=""):
    """Return a usable adb path: saved setting, PATH, then common SDK locations."""
    if saved and os.path.isfile(saved):
        return saved
    on_path = shutil.which("adb")
    if on_path:
        return on_path
    for path in _adb_candidates():
        if os.path.isfile(path):
            return path
    return ""


# --------------------------------------------------------------------------
# Process wrapper
# --------------------------------------------------------------------------

@dataclass
class AdbResult:
    rc: int
    out: str
    err: str
    shown: str

    @property
    def ok(self):
        return self.rc == 0

    @property
    def message(self):
        """Best human readable error text for this result."""
        return self.err.strip() or self.out.strip() or f"adb exited with code {self.rc}"


class Adb:
    def __init__(self, path=""):
        self.path = path or find_adb()

    def display(self, args, serial=None):
        """Return the command as it would be typed, with pairing codes masked."""
        args = [str(a) for a in args]
        if args and args[0] == "pair" and len(args) > 2:
            args[-1] = "******"
        parts = ["adb"]
        if serial:
            parts += ["-s", serial]
        parts += [shlex.quote(a) for a in args]
        return " ".join(parts)

    def _cmd(self, args, serial):
        cmd = [self.path]
        if serial:
            cmd += ["-s", serial]
        cmd += [str(a) for a in args]
        return cmd

    def run(self, args, serial=None, timeout=60):
        """Run adb and return an :class:`AdbResult` (never raises for adb errors)."""
        shown = self.display(args, serial)
        if not self.path:
            return AdbResult(1, "", "adb not found. Pick it from File > Set adb path.", shown)
        try:
            proc = subprocess.run(
                self._cmd(args, serial), capture_output=True, text=True,
                timeout=timeout, encoding="utf-8", errors="replace",
                creationflags=CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            return AdbResult(1, "", f"Timed out after {timeout}s", shown)
        except OSError as exc:
            return AdbResult(1, "", str(exc), shown)
        return AdbResult(proc.returncode, proc.stdout, proc.stderr, shown)

    def run_bytes(self, args, serial=None, timeout=120):
        """Run adb and return ``(rc, stdout_bytes, stderr_text, shown)``."""
        shown = self.display(args, serial)
        if not self.path:
            return 1, b"", "adb not found. Pick it from File > Set adb path.", shown
        try:
            proc = subprocess.run(
                self._cmd(args, serial), capture_output=True,
                timeout=timeout, creationflags=CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            return 1, b"", f"Timed out after {timeout}s", shown
        except OSError as exc:
            return 1, b"", str(exc), shown
        return proc.returncode, proc.stdout, proc.stderr.decode("utf-8", "replace"), shown


# --------------------------------------------------------------------------
# File entries
# --------------------------------------------------------------------------

@dataclass
class Entry:
    name: str
    is_dir: bool
    is_link: bool
    size: int
    modified: str
    link_target: str = ""


# Modern toybox: -rw-rw---- 1 u0_a123 ext_data_rw 1234 2024-05-01 10:23 name
# Old toolbox:    -rw-r--r--   root     root         1234 2013-01-01 12:34 name
# Some builds omit the size for directories, so the metadata block is parsed
# separately and its trailing number (if any) becomes the size.
LS_RE = re.compile(
    r"^(?P<kind>[-dlbcps])[rwxsStT-]{9}[.+@]?\s+"
    r"(?P<meta>.*?)\s+"
    r"(?P<date>\d{4}-\d{2}-\d{2}|[A-Z][a-z]{2}\s+\d{1,2})\s+"
    r"(?P<time>\d{2}:\d{2}|\d{4})\s+"
    r"(?P<name>.+)$"
)
LS_SIZE_RE = re.compile(r"(\d+(?:,\s*\d+)?)\s*$")


def parse_ls(output):
    """Parse ``ls -la`` output into sorted :class:`Entry` objects."""
    entries = []
    for line in output.splitlines():
        match = LS_RE.match(line.strip())
        if not match:
            continue
        kind = match.group("kind")
        meta = match.group("meta").strip()
        size = 0
        size_match = LS_SIZE_RE.search(meta)
        if size_match:
            size = int(size_match.group(1).replace(",", "").replace(" ", ""))
        date = match.group("date")
        time = match.group("time")
        name = match.group("name").rstrip()
        link_target = ""
        if kind == "l" and " -> " in name:
            name, link_target = name.split(" -> ", 1)
            link_target = link_target.strip()
        if name in (".", ".."):
            continue
        entries.append(Entry(
            name=name,
            is_dir=kind in ("d", "l"),  # symlinks are attempted as folders
            is_link=kind == "l",
            size=size,
            modified=f"{date} {time}",
            link_target=link_target,
        ))
    entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
    return entries


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def normalize_remote(path):
    """Normalize a device path. ``~`` maps to /sdcard because adb shell is non-interactive."""
    path = (path or "/").strip()
    if path in ("~", "~/"):
        path = "/sdcard"
    elif path.startswith("~/"):
        path = "/sdcard/" + path[2:]
    if not path.startswith("/"):
        path = "/" + path
    while "//" in path:
        path = path.replace("//", "/")
    if len(path) > 1:
        path = path.rstrip("/")
    return path


def remote_join(base, name):
    base = normalize_remote(base)
    return "/" + name if base == "/" else base + "/" + name


def remote_parent(path):
    path = normalize_remote(path)
    if path == "/":
        return "/"
    return path.rsplit("/", 1)[0] or "/"


# --------------------------------------------------------------------------
# Devices
# --------------------------------------------------------------------------

@dataclass
class Device:
    serial: str
    state: str
    model: str = ""
    product: str = ""
    device: str = ""
    transport: str = ""
    extra: str = ""

    @property
    def display_name(self):
        return self.model.replace("_", " ") if self.model else ""

    @property
    def usable(self):
        return self.state == "device"


def parse_devices(output):
    """Parse ``adb devices -l`` output, ignoring daemon/header noise."""
    devices = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line or line.startswith((
            "List of devices", "*", "adb server", "daemon ", "starting ",
            "error:", "cannot ",
        )):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state, tail = parts[0], parts[1], parts[2:]
        if state == "no" and tail and tail[0] == "permissions":
            state, tail = "no permissions", tail[1:]
        meta, extra = {}, []
        for token in tail:
            key, sep, value = token.partition(":")
            if sep and key in ("usb", "product", "model", "device", "transport_id", "features"):
                meta[key] = value
            else:
                extra.append(token)
        devices.append(Device(
            serial=serial,
            state=state,
            model=meta.get("model", ""),
            product=meta.get("product", ""),
            device=meta.get("device", ""),
            transport=meta.get("usb", ""),
            extra=" ".join(extra),
        ))
    return devices


# --------------------------------------------------------------------------
# Packages
# --------------------------------------------------------------------------

@dataclass
class Package:
    name: str
    apk: str
    system: bool


SYSTEM_PREFIXES = ("/system/", "/product/", "/vendor/", "/apex/", "/system_ext/", "/odm/")


def parse_packages(output):
    """Parse ``pm list packages -f`` output: ``package:<apk>=<name>``."""
    packages = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line.startswith("package:"):
            continue
        apk, sep, name = line[len("package:"):].rpartition("=")
        if not sep or not name:
            continue
        packages.append(Package(
            name=name,
            apk=apk,
            system=apk.startswith(SYSTEM_PREFIXES),
        ))
    packages.sort(key=lambda p: p.name.lower())
    return packages
