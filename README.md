# ADB GUI

A desktop front end for [Android Debug Bridge](https://developer.android.com/tools/adb),
built with Python and PySide6. It wraps the everyday adb commands in a single
window so you do not have to remember flags, serials or shell quoting.

![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![PySide6](https://img.shields.io/badge/PySide6-6.x-green)

## Features

- **Devices** - USB and wireless devices with state, model and serial; automatic
  refresh, manual refresh (F5), disconnect and adb server start/kill.
- **Wireless** - `adb connect` and Android 11+ `adb pair` (pairing codes are
  masked in the command log).
- **Files** - browse device storage, pull/push files and folders, drag & drop
  from Explorer, create folders, rename and delete. Handles paths with spaces
  safely and supports symlinks.
- **Shell** - raw `adb shell` commands with history (Up/Down), a persistent
  device working directory, `cd` support and example commands.
- **Apps** - list user/system packages, filter, install APKs, launch, force
  stop, clear data, open Android app settings and uninstall.
- **Logcat** - live streaming from the device with a case-insensitive filter,
  auto-scroll, clear and save.
- **Device tools** - screenshot with preview/save, device info (model, Android
  version, ABI, screen, battery, storage, Wi-Fi IP) and reboot to system,
  recovery or bootloader.
- **Command log** - every adb command and its output, so the GUI doubles as a
  way to learn the commands.

All device work happens on background threads, so the window stays responsive
during large transfers and directory listings. Results from outdated requests
are discarded when you switch devices or folders quickly.

## Requirements

- Python 3.9 or newer
- [Android platform-tools](https://developer.android.com/tools/releases/platform-tools)
  (`adb`)
- `PySide6` (installed from `requirements.txt`)

The app looks for adb in this order: the saved path, `PATH`, then common SDK
locations such as `C:\platform-tools\adb.exe` and
`%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe`. You can always pick the
executable with **File > Set adb path...**.

## Run from source

```powershell
python -m pip install -r requirements.txt
python adb_gui.py
```

## Wireless debugging

1. On the phone, enable **Developer options > Wireless debugging**.
2. The first time, use **Pair device with pairing code**. Enter the
   `ip:port` and 6-digit code shown on the phone in the **Pair** fields.
3. After pairing, connect with the `ip:port` shown under Wireless debugging
   (the pairing port is not the connect port; usually the connect address ends
   in `:5555`).

If a USB device shows `unauthorized`, unlock the phone and accept the USB
debugging prompt. For `no permissions` on Linux, fix your udev rules.

## Keyboard shortcuts

| Shortcut | Action |
| --- | --- |
| `F5` | Refresh devices |
| `Ctrl+L` | Focus the device path bar |
| `Ctrl+T` | Focus the shell input |
| `Ctrl+Shift+S` | Take a screenshot |
| `Ctrl+I` | Install an APK |
| `Ctrl+D` | Show device info |
| `Ctrl+Q` | Quit |
| `Backspace` (file table) | Up one folder |
| `Enter` (file table) | Open the selected folder |
| `Delete` (file table) | Delete selected items |
| `Up` / `Down` (shell) | Command history |

## Build a standalone Windows executable

```powershell
.\build.ps1
```

The result is `dist\ADB-GUI.exe` (a single windowed executable). Android
platform-tools are **not** bundled; the app still needs `adb.exe` on the machine,
either on `PATH`, in a common SDK location, or selected from the toolbar.

You can also run PyInstaller directly:

```powershell
python -m pip install pyinstaller
python -m PyInstaller --noconfirm --clean adb-gui.spec
```

## Tests

The parsing helpers are covered by unit tests:

```powershell
python -m unittest -v
```

## Project layout

```
adb_gui.py        PySide6 window, tabs, dialogs, workers
adb_core.py       adb process wrapper and output parsers (no Qt dependency)
test_adb_core.py  unit tests for device/package/ls/path parsing
adb-gui.spec      PyInstaller one-file build definition
build.ps1         convenience build script
```

## Notes and limitations

- `adb shell` runs one command per request, so environment variables and `cd`
  changes do not persist between commands. The Shell tab tracks its own working
  directory to compensate.
- Logcat uses a background reader thread and keeps the last 20,000 lines in
  memory. Saving writes the currently visible (filtered) lines.
- File listings use `ls -la`; unusual permission bits or heavily customized
  Android shells may not parse perfectly.
- Delete, push, clear-data and uninstall actions change the device. They ask for
  confirmation where it matters, but use the app at your own risk.

## License

No license file is included yet. Add one before distributing the project if you
intend it to be open source.
