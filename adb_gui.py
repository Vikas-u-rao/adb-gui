#!/usr/bin/env python3
"""
ADB GUI - a desktop front end for Android platform-tools.

Features
  * Device list (USB + wireless), live refresh, disconnect
  * Wireless connect and pairing (Android 11+)
  * File manager: browse, pull, push, drag & drop, mkdir, rename, delete
  * Raw shell tab with history and a persistent device working directory
  * App manager: list/filter, install APK, launch, force stop, clear data,
    uninstall, open Android app settings
  * Streaming logcat viewer with a text filter and save support
  * Screenshot capture with preview, device info and reboot actions
  * Command log showing every adb invocation

Requirements
  pip install -r requirements.txt
  adb on PATH or in a common SDK location, or pick it from File > Set adb path.

Run
  python adb_gui.py
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import threading
import traceback
from collections import deque
from dataclasses import dataclass
from datetime import datetime

from PySide6 import __version__ as PYSIDE_VERSION
from PySide6.QtCore import (
    QObject, QPointF, QRectF, QRunnable, QSettings, QSize, QThreadPool, QTimer,
    Qt, Signal, Slot,
)
from PySide6.QtGui import (
    QAction, QColor, QFontDatabase, QIcon, QKeySequence, QPainter,
    QPainterPath, QPalette, QPen, QPixmap, QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QSizePolicy,
    QSplitter, QStyle, QTableWidget, QTableWidgetItem, QTabWidget, QToolBar,
    QToolButton, QVBoxLayout, QWidget,
)

from adb_core import (
    Adb, Device, Package, human_size, normalize_remote, parse_devices, parse_ls,
    parse_packages, remote_join, remote_parent,
)

APP_NAME = "ADB GUI"
__version__ = "1.0.0"


# --------------------------------------------------------------------------
# Background worker
# --------------------------------------------------------------------------

class WorkerSignals(QObject):
    done = Signal(int, object)


class Worker(QRunnable):
    """Runs one adb call and reports back with a task id."""

    def __init__(self, task_id, fn):
        super().__init__()
        self.task_id = task_id
        self.fn = fn
        self.signals = WorkerSignals()

    def run(self):
        try:
            result = self.fn()
        except Exception as exc:  # noqa: BLE001 - reported in the UI
            result = exc
        self.signals.done.emit(self.task_id, result)


@dataclass
class _Task:
    after: object = None
    log_output: bool = True
    binary: bool = False


class LogcatStream(QObject):
    """Streams ``adb logcat`` from a background thread without a console window."""

    started = Signal()
    lines = Signal(list)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, adb_path, serial, parent=None):
        super().__init__(parent)
        self.adb_path = adb_path
        self.serial = serial
        self._proc = None
        self._thread = None
        self._stopping = False

    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.running():
            return
        self._stopping = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        command = [self.adb_path, "-s", self.serial, "logcat", "-v", "time"]
        try:
            self._proc = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, creationflags=0x08000000 if os.name == "nt" else 0,
            )
        except OSError as exc:
            self.failed.emit(str(exc))
            self.finished.emit()
            return

        self.started.emit()
        partial = ""
        try:
            while not self._stopping:
                data = self._proc.stdout.read1(8192)
                if not data:
                    break
                partial += data.decode("utf-8", "replace")
                lines = partial.split("\n")
                partial = lines.pop()
                if lines:
                    self.lines.emit([line.rstrip("\r") for line in lines])
            if partial.strip() and not self._stopping:
                self.lines.emit([partial.rstrip("\r")])
        except (OSError, ValueError):
            pass
        finally:
            try:
                self._proc.wait(timeout=2)
            except Exception:  # noqa: BLE001 - best effort cleanup
                pass
            self.finished.emit()

    def stop(self):
        self._stopping = True
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.5)
        self._thread = None


def monospace(widget):
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    font.setPointSize(10)
    widget.setFont(font)


def dim_label(text, parent=None):
    label = QLabel(text, parent)
    label.setWordWrap(True)
    label.setStyleSheet(
        f"color: {label.palette().color(QPalette.ColorRole.PlaceholderText).name()};")
    return label


def is_dark(widget):
    return widget.palette().color(QPalette.ColorRole.Window).lightness() < 128


def danger_color(widget):
    return "#f85149" if is_dark(widget) else "#cf222e"


def state_colors(widget):
    if is_dark(widget):
        return {
            "device": ("#3fb950", "connected"),
            "unauthorized": ("#d29922", "unauthorized"),
            "offline": ("#8b949e", "offline"),
            "no permissions": ("#f85149", "no permission"),
            "recovery": ("#58a6ff", "recovery"),
            "bootloader": ("#58a6ff", "bootloader"),
            "sideload": ("#58a6ff", "sideload"),
        }
    return {
        "device": ("#1a7f37", "connected"),
        "unauthorized": ("#9a6700", "unauthorized"),
        "offline": ("#57606a", "offline"),
        "no permissions": ("#cf222e", "no permission"),
        "recovery": ("#0969da", "recovery"),
        "bootloader": ("#0969da", "bootloader"),
        "sideload": ("#0969da", "sideload"),
    }


def _glyph_path(painter, name):
    """Draw one icon in a 24x24 coordinate box."""
    if name == "folder":
        path = QPainterPath()
        path.moveTo(3.5, 19.5)
        path.lineTo(3.5, 6.5)
        path.lineTo(9.5, 6.5)
        path.lineTo(12.0, 9.0)
        path.lineTo(20.5, 9.0)
        path.lineTo(20.5, 19.5)
        path.closeSubpath()
        painter.drawPath(path)
    elif name == "refresh":
        painter.drawArc(QRectF(5.0, 5.0, 14.0, 14.0), 120 * 16, 300 * 16)
        path = QPainterPath()
        path.moveTo(14.2, 3.2)
        path.lineTo(18.8, 5.6)
        path.lineTo(14.8, 8.3)
        path.closeSubpath()
        painter.save()
        painter.setBrush(painter.pen().color())
        painter.drawPath(path)
        painter.restore()
    elif name == "camera":
        painter.drawRoundedRect(QRectF(3.0, 8.0, 18.0, 12.0), 2.5, 2.5)
        path = QPainterPath()
        path.moveTo(8.0, 8.0)
        path.lineTo(9.3, 5.5)
        path.lineTo(14.7, 5.5)
        path.lineTo(16.0, 8.0)
        painter.drawPath(path)
        painter.drawEllipse(QPointF(12.0, 14.0), 4.0, 4.0)
    elif name == "download":
        painter.drawLine(QPointF(12.0, 3.5), QPointF(12.0, 14.0))
        path = QPainterPath()
        path.moveTo(7.5, 9.5)
        path.lineTo(12.0, 14.0)
        path.lineTo(16.5, 9.5)
        painter.drawPath(path)
        path = QPainterPath()
        path.moveTo(4.0, 16.5)
        path.lineTo(4.0, 20.0)
        path.lineTo(20.0, 20.0)
        path.lineTo(20.0, 16.5)
        painter.drawPath(path)
    elif name == "power":
        painter.drawArc(QRectF(5.5, 5.5, 13.0, 13.0), 120 * 16, 300 * 16)
        painter.drawLine(QPointF(12.0, 3.5), QPointF(12.0, 11.5))
    elif name == "info":
        painter.drawEllipse(QPointF(12.0, 12.0), 8.8, 8.8)
        painter.save()
        painter.setBrush(painter.pen().color())
        painter.drawEllipse(QPointF(12.0, 7.6), 1.1, 1.1)
        painter.restore()
        painter.drawLine(QPointF(12.0, 11.0), QPointF(12.0, 17.2))
    elif name == "play":
        path = QPainterPath()
        path.moveTo(8.0, 5.0)
        path.lineTo(19.0, 12.0)
        path.lineTo(8.0, 19.0)
        path.closeSubpath()
        painter.save()
        painter.setBrush(painter.pen().color())
        painter.drawPath(path)
        painter.restore()
    elif name == "stop":
        painter.save()
        painter.setBrush(painter.pen().color())
        painter.drawRoundedRect(QRectF(7.0, 7.0, 10.0, 10.0), 1.5, 1.5)
        painter.restore()
    elif name == "save":
        painter.drawRoundedRect(QRectF(4.0, 4.0, 16.0, 16.0), 2.0, 2.0)
        path = QPainterPath()
        path.moveTo(8.0, 4.0)
        path.lineTo(8.0, 10.0)
        path.lineTo(16.0, 10.0)
        path.lineTo(16.0, 4.0)
        painter.drawPath(path)
        painter.drawRect(QRectF(7.5, 13.0, 9.0, 7.0))
    elif name == "trash":
        painter.drawLine(QPointF(9.0, 5.0), QPointF(15.0, 5.0))
        painter.drawLine(QPointF(4.5, 8.0), QPointF(19.5, 8.0))
        path = QPainterPath()
        path.moveTo(7.0, 8.0)
        path.lineTo(8.0, 20.0)
        path.lineTo(16.0, 20.0)
        path.lineTo(17.0, 8.0)
        painter.drawPath(path)
        painter.drawLine(QPointF(10.5, 11.0), QPointF(10.5, 17.0))
        painter.drawLine(QPointF(13.5, 11.0), QPointF(13.5, 17.0))
    elif name == "up":
        painter.drawLine(QPointF(12.0, 4.0), QPointF(12.0, 20.0))
        path = QPainterPath()
        path.moveTo(6.0, 10.0)
        path.lineTo(12.0, 4.0)
        path.lineTo(18.0, 10.0)
        painter.drawPath(path)
    elif name == "clear":
        painter.drawEllipse(QPointF(12.0, 12.0), 8.5, 8.5)
        painter.drawLine(QPointF(9.0, 9.0), QPointF(15.0, 15.0))
        painter.drawLine(QPointF(15.0, 9.0), QPointF(9.0, 15.0))
    elif name == "copy":
        painter.drawRoundedRect(QRectF(4.0, 4.0, 11.0, 13.0), 2.0, 2.0)
        painter.drawRoundedRect(QRectF(9.0, 8.0, 11.0, 13.0), 2.0, 2.0)


def glyph_icon(name, color):
    icon = QIcon()
    for size in (16, 20, 24, 32, 48, 64):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.scale(size / 24.0, size / 24.0)
        pen = QPen(QColor(color))
        pen.setWidthF(1.7)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        _glyph_path(painter, name)
        painter.end()
        icon.addPixmap(pixmap)
    return icon


# --------------------------------------------------------------------------
# Small widgets
# --------------------------------------------------------------------------

class HistoryLineEdit(QLineEdit):
    """Line edit with shell-like up/down history."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.history = []
        self._index = 0

    def remember(self, text):
        if text and (not self.history or self.history[-1] != text):
            self.history.append(text)
        self._index = len(self.history)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Up and self.history:
            self._index = max(0, self._index - 1)
            self.setText(self.history[self._index])
            return
        if event.key() == Qt.Key.Key_Down and self.history:
            self._index = min(len(self.history), self._index + 1)
            self.setText("" if self._index == len(self.history) else self.history[self._index])
            return
        super().keyPressEvent(event)


class ElidedLabel(QLabel):
    """QLabel that elides its text instead of forcing the layout wider."""

    def __init__(self, text="", mode=Qt.TextElideMode.ElideMiddle, parent=None):
        super().__init__(parent)
        self._full_text = text
        self._mode = mode
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)
        self.setToolTip(text)
        self.setText(text)

    def setText(self, text):
        self._full_text = text or ""
        self.setToolTip(self._full_text)
        super().setText(self._full_text)
        self._apply_elide()

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        return QSize(0, hint.height())

    def sizeHint(self):
        hint = super().sizeHint()
        width = self.fontMetrics().horizontalAdvance(self._full_text) + 2
        return QSize(min(width, self.maximumWidth()), hint.height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_elide()

    def _apply_elide(self):
        if self.width() <= 0:
            return
        text = self.fontMetrics().elidedText(self._full_text, self._mode, self.width())
        if text != super().text():
            super().setText(text)


class DeviceRow(QWidget):
    """Two-line device card: model, serial and a colored state badge."""

    def __init__(self, dev, parent=None):
        super().__init__(parent)
        self._state_color, label = state_colors(self).get(
            dev.state, ("#8b949e", dev.state))
        self._selected = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 7, 10, 7)
        layout.setSpacing(8)

        self._dot = QLabel("\u25cf")
        self._dot.setFixedWidth(12)
        self._dot.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self._dot)

        text = QVBoxLayout()
        text.setSpacing(1)
        self._name = ElidedLabel(dev.display_name or "Unknown device",
                                 Qt.TextElideMode.ElideRight)
        name_font = self._name.font()
        name_font.setBold(True)
        self._name.setFont(name_font)
        self._serial = ElidedLabel(dev.serial, Qt.TextElideMode.ElideMiddle)
        serial_font = self._serial.font()
        serial_font.setPointSizeF(max(7.5, serial_font.pointSizeF() - 1.0))
        self._serial.setFont(serial_font)
        text.addWidget(self._name)
        text.addWidget(self._serial)
        layout.addLayout(text, 1)

        self._state = QLabel(label)
        self._state.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self._state)

        detail = [f"serial: {dev.serial}", f"state: {dev.state}"]
        for name, value in (("model", dev.model), ("product", dev.product),
                            ("device", dev.device), ("usb", dev.transport)):
            if value:
                detail.append(f"{name}: {value}")
        if dev.extra:
            detail.append(dev.extra)
        self.setToolTip("\n".join(detail))
        self._apply_colors()

    def set_selected(self, selected):
        if self._selected != selected:
            self._selected = selected
            self._apply_colors()

    def _apply_colors(self):
        if self._selected:
            text_color = self.palette().color(QPalette.ColorRole.HighlightedText).name()
            serial_color = text_color
            dot_color = text_color
        else:
            text_color = self.palette().color(QPalette.ColorRole.WindowText).name()
            serial_color = self.palette().color(QPalette.ColorRole.PlaceholderText).name()
            dot_color = self._state_color
        self._name.setStyleSheet(f"color: {text_color};")
        self._serial.setStyleSheet(f"color: {serial_color};")
        self._state.setStyleSheet(
            f"color: {text_color}; font-size: 11px; font-weight: 600;")
        self._dot.setStyleSheet(f"color: {dot_color}; font-size: 14px;")


class FileTable(QTableWidget):
    """File table that accepts local files dropped from Explorer."""

    files_dropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(0, 3, parent)
        self.setAcceptDrops(True)

    @staticmethod
    def _local_paths(event):
        mime = event.mimeData()
        if not mime.hasUrls():
            return []
        return [url.toLocalFile() for url in mime.urls() if url.isLocalFile()]

    def dragEnterEvent(self, event):
        if self._local_paths(event):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if self._local_paths(event):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        paths = self._local_paths(event)
        if paths:
            self.files_dropped.emit(paths)
            event.acceptProposedAction()
            return
        super().dropEvent(event)


# --------------------------------------------------------------------------
# Dialogs
# --------------------------------------------------------------------------

class ScreenshotDialog(QDialog):
    def __init__(self, data: bytes, serial: str, parent=None):
        super().__init__(parent)
        self.data = data
        self.setWindowTitle(f"Screenshot - {serial}")
        self.resize(940, 700)

        self.pixmap = QPixmap()
        if not self.pixmap.loadFromData(data):
            alt = data.replace(b"\r\n", b"\n")  # old adb exec-out workaround
            if alt != data:
                self.pixmap.loadFromData(alt)
                if not self.pixmap.isNull():
                    self.data = alt

        image = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        if self.pixmap.isNull():
            image.setText("Could not decode the screenshot data.")
        else:
            image.setPixmap(self.pixmap.scaled(
                QSize(900, 620), Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))
        image.setMinimumSize(320, 240)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        save = buttons.addButton("Save as...", QDialogButtonBox.ButtonRole.ActionRole)
        save.clicked.connect(self.save)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(image, 1)
        layout.addWidget(buttons)

    def save(self):
        default = os.path.join(
            os.path.expanduser("~"), "Pictures",
            f"screenshot_{datetime.now():%Y%m%d_%H%M%S}.png",
        )
        path, _ = QFileDialog.getSaveFileName(self, "Save screenshot", default,
                                             "PNG image (*.png);;All files (*)")
        if not path:
            return
        if not path.lower().endswith(".png"):
            path += ".png"
        try:
            with open(path, "wb") as handle:
                handle.write(self.data)
        except OSError as exc:
            QMessageBox.warning(self, "Save failed", str(exc))


INFO_FIELDS = [
    ("model", "Model"),
    ("brand", "Brand"),
    ("android", "Android version"),
    ("sdk", "SDK level"),
    ("abi", "CPU ABI"),
    ("serialno", "Device serial"),
    ("screen", "Screen"),
    ("density", "Density"),
    ("battery", "Battery"),
    ("storage", "Data storage"),
    ("wifi", "Wi-Fi IP"),
]


class DeviceInfoDialog(QDialog):
    def __init__(self, serial: str, info: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Device info - {serial}")
        self.resize(620, 520)
        self._text = f"ADB serial: {serial}\n"

        form = QFormLayout()
        form.addRow("ADB serial", QLabel(serial))
        for key, label in INFO_FIELDS:
            value = (info.get(key) or "").strip()
            if key == "wifi":
                value = value.replace("inet ", "").strip()
            value = value or "-"
            widget = QLabel(value)
            widget.setWordWrap(True)
            widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            form.addRow(label, widget)
            self._text += f"{label}: {value}\n"

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        copy = buttons.addButton("Copy", QDialogButtonBox.ButtonRole.ActionRole)
        copy.clicked.connect(self._copy)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addStretch(1)
        layout.addWidget(buttons)

    def _copy(self):
        QApplication.clipboard().setText(self._text)


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))
        self.resize(1240, 800)
        self.setMinimumSize(980, 640)

        self.pool = QThreadPool.globalInstance()
        settings = QSettings()
        self.adb = Adb(str(settings.value("adb_path", "") or ""))

        self.serial = None
        self.selected_device = None
        self.cwd = "/sdcard"
        self.shell_cwd = "/sdcard"
        self.entries = []
        self._device_sig = None
        self._refreshing = False

        self._task_seq = 0
        self._tasks = {}
        self._workers = {}

        self._danger = danger_color(self)
        self._icon = lambda name: glyph_icon(
            name, self.palette().color(QPalette.ColorRole.WindowText).name())
        self._danger_icon = lambda name: glyph_icon(name, self._danger)

        self._dir_gen = 0
        self._apps_loaded = False
        self._apps_loading = False
        self._all_packages = []

        self._logcat_lines = deque(maxlen=20000)
        self._logcat_failed = False

        self._build_actions()
        self._build_menus()
        self._build_toolbar()
        self._build_ui()

        self._restore_settings()
        self._update_device_label()

        self._auto_timer = QTimer(self)
        self._auto_timer.setInterval(5000)
        self._auto_timer.timeout.connect(lambda: self.refresh_devices(quiet=True))
        self._auto_timer.start()

        self.refresh_devices()
        if not self.adb.path:
            self.log_line("[adb] not found. Use File > Set adb path.")

    # ------------------------------------------------------------- actions
    def _action(self, text, slot, shortcut=None):
        action = QAction(text, self)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        action.triggered.connect(slot)
        return action

    def _build_actions(self):
        self.act_set_adb = self._action("Set adb path...", self.pick_adb)
        self.act_set_adb.setIcon(self._icon("folder"))
        self.act_start_server = self._action("Start adb server", self.start_server)
        self.act_kill_server = self._action("Kill adb server", self.kill_server)
        self.act_refresh = self._action("Refresh devices", self.refresh_devices, "F5")
        self.act_refresh.setIcon(self._icon("refresh"))
        self.act_disconnect = self._action("Disconnect wireless device", self.disconnect_device)
        self.act_screenshot = self._action("Screenshot", self.take_screenshot, "Ctrl+Shift+S")
        self.act_screenshot.setIcon(self._icon("camera"))
        self.act_install = self._action("Install APK...", self.install_apk, "Ctrl+I")
        self.act_install.setIcon(self._icon("download"))
        self.act_device_info = self._action("Device info", self.show_device_info, "Ctrl+D")
        self.act_device_info.setIcon(self._icon("info"))
        self.act_quit = self._action("Exit", self.close, "Ctrl+Q")

    def _build_menus(self):
        bar = self.menuBar()

        file_menu = bar.addMenu("&File")
        file_menu.addAction(self.act_set_adb)
        file_menu.addSeparator()
        file_menu.addAction(self.act_start_server)
        file_menu.addAction(self.act_kill_server)
        file_menu.addSeparator()
        file_menu.addAction(self.act_quit)

        device_menu = bar.addMenu("&Device")
        device_menu.addAction(self.act_refresh)
        device_menu.addAction(self.act_disconnect)
        device_menu.addSeparator()
        device_menu.addAction(self.act_screenshot)
        device_menu.addAction(self.act_install)
        device_menu.addAction(self.act_device_info)
        reboot_menu = device_menu.addMenu("Reboot")
        self._fill_reboot_menu(reboot_menu)

        help_menu = bar.addMenu("&Help")
        about = self._action("About", self.show_about)
        help_menu.addAction(about)

    def _fill_reboot_menu(self, menu):
        for label, mode in (("System", ""), ("Recovery", "recovery"),
                            ("Bootloader", "bootloader")):
            action = menu.addAction(label)
            action.triggered.connect(lambda _=False, m=mode: self.reboot(m))

    def _build_toolbar(self):
        toolbar = QToolBar("Main", self)
        toolbar.setObjectName("MainToolbar")
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(toolbar)

        toolbar.addAction(self.act_set_adb)
        toolbar.addSeparator()
        toolbar.addAction(self.act_refresh)
        toolbar.addAction(self.act_screenshot)
        toolbar.addAction(self.act_install)

        reboot_button = QToolButton()
        reboot_button.setText("Reboot")
        reboot_button.setIcon(self._icon("power"))
        reboot_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        reboot_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        reboot_button.setToolTip("Reboot the device")
        reboot_menu = QMenu(reboot_button)
        self._fill_reboot_menu(reboot_menu)
        reboot_button.setMenu(reboot_menu)
        toolbar.addWidget(reboot_button)

        toolbar.addAction(self.act_device_info)

    # ----------------------------------------------------------------- UI
    def _build_ui(self):
        central = QSplitter(Qt.Orientation.Vertical)
        self.top_split = QSplitter(Qt.Orientation.Horizontal)
        self.top_split.addWidget(self._build_side_panel())

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setIconSize(QSize(16, 16))
        self.files_tab = self._build_files_tab()
        self.shell_tab = self._build_shell_tab()
        self.apps_tab = self._build_apps_tab()
        self.logcat_tab = self._build_logcat_tab()
        self.tabs.addTab(self.files_tab, "Files")
        self.tabs.addTab(self.shell_tab, "Shell")
        self.tabs.addTab(self.apps_tab, "Apps")
        self.tabs.addTab(self.logcat_tab, "Logcat")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.top_split.addWidget(self.tabs)
        self.top_split.setStretchFactor(0, 0)
        self.top_split.setStretchFactor(1, 1)
        self.top_split.setSizes([340, 900])
        central.addWidget(self.top_split)
        central.addWidget(self._build_log_panel())
        central.setSizes([560, 180])

        self.main_split = central
        self.setCentralWidget(central)

        status = self.statusBar()
        self.status = status
        self.device_label = ElidedLabel(
            "No device selected", Qt.TextElideMode.ElideMiddle)
        self.device_label.setMaximumWidth(560)
        status.addPermanentWidget(self.device_label)

        self._shortcuts = [
            QShortcut(QKeySequence("Ctrl+L"), self),
            QShortcut(QKeySequence("Ctrl+T"), self),
        ]
        self._shortcuts[0].activated.connect(self._focus_path)
        self._shortcuts[1].activated.connect(self._focus_shell)

    def _build_side_panel(self):
        panel = QWidget()
        panel.setMinimumWidth(320)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(10)

        devices_box = QGroupBox("Devices")
        devices_layout = QVBoxLayout(devices_box)
        self.device_list = QListWidget()
        self.device_list.setSpacing(2)
        self.device_list.setUniformItemSizes(True)
        self.device_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.device_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.device_list.currentItemChanged.connect(self.on_device_selected)
        devices_layout.addWidget(self.device_list, 1)

        row = QHBoxLayout()
        refresh = QPushButton("Refresh")
        refresh.setIcon(self._icon("refresh"))
        refresh.clicked.connect(lambda: self.refresh_devices())
        row.addWidget(refresh)
        disconnect = QPushButton("Disconnect")
        disconnect.clicked.connect(self.disconnect_device)
        row.addWidget(disconnect)
        devices_layout.addLayout(row)
        layout.addWidget(devices_box, 1)

        wifi_box = QGroupBox("Wireless")
        wifi_layout = QVBoxLayout(wifi_box)
        wifi_layout.setSpacing(6)
        wifi_layout.addWidget(QLabel("Connect (ip:port)"))
        self.connect_edit = QLineEdit()
        self.connect_edit.setPlaceholderText("192.168.1.20:5555")
        self.connect_edit.returnPressed.connect(self.connect_device)
        wifi_layout.addWidget(self.connect_edit)
        connect = QPushButton("Connect")
        connect.clicked.connect(self.connect_device)
        wifi_layout.addWidget(connect)

        wifi_layout.addWidget(QLabel("Pair (Android 11+)"))
        self.pair_addr = QLineEdit()
        self.pair_addr.setPlaceholderText("192.168.1.20:37099")
        wifi_layout.addWidget(self.pair_addr)
        self.pair_code = QLineEdit()
        self.pair_code.setPlaceholderText("6-digit pairing code")
        self.pair_code.returnPressed.connect(self.pair_device)
        wifi_layout.addWidget(self.pair_code)
        pair = QPushButton("Pair")
        pair.clicked.connect(self.pair_device)
        wifi_layout.addWidget(pair)

        hint = dim_label("Enable Wireless debugging on the phone, then use the "
                         "pairing ip:port and code shown there.")
        wifi_layout.addWidget(hint)
        layout.addWidget(wifi_box)
        return panel

    def _build_files_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        nav = QHBoxLayout()
        nav.setSpacing(6)
        up = QPushButton()
        up.setIcon(self._icon("up"))
        up.setToolTip("Up one folder (Backspace)")
        up.clicked.connect(self.go_up)
        nav.addWidget(up)
        reload_button = QPushButton()
        reload_button.setIcon(self._icon("refresh"))
        reload_button.setToolTip("Reload (F5 refreshes devices)")
        reload_button.clicked.connect(lambda: self.load_dir())
        nav.addWidget(reload_button)

        self.path_edit = QLineEdit(self.cwd)
        self.path_edit.returnPressed.connect(self.goto_typed_path)
        nav.addWidget(self.path_edit, 1)

        for label, path in (("sdcard", "/sdcard"), ("Download", "/sdcard/Download"),
                            ("DCIM", "/sdcard/DCIM"), ("/", "/")):
            button = QPushButton(label)
            button.clicked.connect(lambda _=False, p=path: self.goto(p))
            nav.addWidget(button)
        layout.addLayout(nav)

        self.file_table = FileTable()
        self.file_table.setHorizontalHeaderLabels(["Name", "Size", "\u00a0\u00a0Modified"])
        self.file_table.horizontalHeaderItem(0).setTextAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.file_table.horizontalHeaderItem(1).setTextAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.file_table.horizontalHeaderItem(2).setTextAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header = self.file_table.horizontalHeader()
        header.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(1, 96)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(2, 150)
        header.setHighlightSections(False)
        self.file_table.verticalHeader().setVisible(False)
        self.file_table.verticalHeader().setDefaultSectionSize(27)
        self.file_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.file_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.file_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.file_table.setAlternatingRowColors(True)
        self.file_table.setShowGrid(False)
        self.file_table.setWordWrap(False)
        self.file_table.cellDoubleClicked.connect(self.on_open)
        self.file_table.files_dropped.connect(self.push_paths)
        self.file_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.file_table.customContextMenuRequested.connect(self._files_menu)
        layout.addWidget(self.file_table, 1)

        delete_shortcut = QShortcut(QKeySequence("Delete"), self.file_table)
        delete_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        delete_shortcut.activated.connect(self.delete_selected)
        back_shortcut = QShortcut(QKeySequence("Backspace"), self.file_table)
        back_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        back_shortcut.activated.connect(self.go_up)
        enter_shortcut = QShortcut(QKeySequence("Return"), self.file_table)
        enter_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        enter_shortcut.activated.connect(self._open_current)
        self._file_shortcuts = [delete_shortcut, back_shortcut, enter_shortcut]

        actions = QHBoxLayout()
        actions.setSpacing(6)
        for label, slot in (("Pull to PC", self.pull_selected),
                            ("Push file(s)", self.push_files),
                            ("Push folder", self.push_folder),
                            ("New folder", self.new_folder),
                            ("Rename", self.rename_selected),
                            ("Delete", self.delete_selected)):
            button = QPushButton(label)
            button.clicked.connect(slot)
            if label == "Delete":
                button.setStyleSheet(f"color: {self._danger};")
                button.setIcon(self._danger_icon("trash"))
            actions.addWidget(button)
        layout.addLayout(actions)
        return page

    def _build_shell_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        self.shell_output = QPlainTextEdit()
        self.shell_output.setReadOnly(True)
        self.shell_output.setMaximumBlockCount(10000)
        self.shell_output.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.shell_output.setPlaceholderText(
            "Commands run with adb shell. This is a raw shell, so pipes and ; work."
        )
        monospace(self.shell_output)
        layout.addWidget(self.shell_output, 1)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.shell_prompt = QLabel(f"{self.shell_cwd} $")
        monospace(self.shell_prompt)
        row.addWidget(self.shell_prompt)
        self.shell_input = HistoryLineEdit()
        self.shell_input.setPlaceholderText("e.g. getprop ro.product.model")
        self.shell_input.returnPressed.connect(self.run_shell)
        monospace(self.shell_input)
        row.addWidget(self.shell_input, 1)
        run = QPushButton("Run")
        run.setIcon(self._icon("play"))
        run.clicked.connect(self.run_shell)
        row.addWidget(run)
        clear = QPushButton("Clear")
        clear.setIcon(self._icon("clear"))
        clear.clicked.connect(lambda: self.shell_output.clear())
        row.addWidget(clear)
        layout.addLayout(row)

        extras = QHBoxLayout()
        extras.setSpacing(6)
        extras.addWidget(QLabel("Examples:"))
        self.shell_examples = QComboBox()
        self.shell_examples.addItems([
            "Choose...",
            "getprop ro.product.model",
            "df -h",
            "ps -A",
            "dumpsys battery",
            "pm list packages -3",
            "ip addr show wlan0",
        ])
        self.shell_examples.activated.connect(self._use_shell_example)
        extras.addWidget(self.shell_examples)
        extras.addStretch(1)
        layout.addLayout(extras)
        return page

    def _build_apps_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(6)
        self.apps_filter = QLineEdit()
        self.apps_filter.setPlaceholderText("Filter packages...")
        self.apps_filter.textChanged.connect(lambda _text: self._populate_apps())
        top.addWidget(self.apps_filter, 1)
        self.apps_system = QCheckBox("Show system apps")
        self.apps_system.stateChanged.connect(lambda _state: self._populate_apps())
        top.addWidget(self.apps_system)
        refresh = QPushButton("Refresh")
        refresh.setIcon(self._icon("refresh"))
        refresh.clicked.connect(self.load_apps)
        top.addWidget(refresh)
        install = QPushButton("Install APK...")
        install.setIcon(self._icon("download"))
        install.clicked.connect(self.install_apk)
        top.addWidget(install)
        layout.addLayout(top)

        self.apps_table = QTableWidget(0, 2)
        self.apps_table.setHorizontalHeaderLabels(["Package", "Type"])
        self.apps_table.horizontalHeaderItem(0).setTextAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.apps_table.horizontalHeaderItem(1).setTextAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header = self.apps_table.horizontalHeader()
        header.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setHighlightSections(False)
        self.apps_table.verticalHeader().setVisible(False)
        self.apps_table.verticalHeader().setDefaultSectionSize(27)
        self.apps_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.apps_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.apps_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.apps_table.setAlternatingRowColors(True)
        self.apps_table.setShowGrid(False)
        self.apps_table.setWordWrap(False)
        self.apps_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.apps_table.customContextMenuRequested.connect(self._apps_menu)
        self.apps_table.cellDoubleClicked.connect(lambda _r, _c: self.launch_app())
        layout.addWidget(self.apps_table, 1)

        actions = QHBoxLayout()
        actions.setSpacing(6)
        for label, slot in (("Launch", self.launch_app),
                            ("Force stop", self.force_stop_app),
                            ("Clear data", self.clear_app_data),
                            ("App settings", self.open_app_settings),
                            ("Uninstall", self.uninstall_app)):
            button = QPushButton(label)
            button.clicked.connect(slot)
            if label in ("Clear data", "Uninstall"):
                button.setStyleSheet(f"color: {self._danger};")
                button.setIcon(self._danger_icon("trash"))
            actions.addWidget(button)
        self.apps_count = QLabel("0 packages")
        self.apps_count.setStyleSheet(
            f"color: {self.palette().color(QPalette.ColorRole.PlaceholderText).name()};")
        actions.addStretch(1)
        actions.addWidget(self.apps_count)
        layout.addLayout(actions)
        return page

    def _build_logcat_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(6)
        self.logcat_btn = QPushButton("Start")
        self.logcat_btn.setIcon(self._icon("play"))
        self.logcat_btn.clicked.connect(self._toggle_logcat)
        top.addWidget(self.logcat_btn)
        clear = QPushButton("Clear")
        clear.setIcon(self._icon("clear"))
        clear.clicked.connect(self.clear_logcat)
        top.addWidget(clear)
        save = QPushButton("Save...")
        save.setIcon(self._icon("save"))
        save.clicked.connect(self.save_logcat)
        top.addWidget(save)
        self.logcat_auto = QCheckBox("Auto-scroll")
        self.logcat_auto.setChecked(True)
        top.addWidget(self.logcat_auto)
        top.addStretch(1)
        self.logcat_status = QLabel("stopped")
        self.logcat_status.setMinimumWidth(64)
        self.logcat_status.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        top.addWidget(self.logcat_status)
        top.addSpacing(12)
        top.addWidget(QLabel("Filter:"))
        self.logcat_filter = QLineEdit()
        self.logcat_filter.setPlaceholderText("case-insensitive text...")
        self.logcat_filter.textChanged.connect(lambda _text: self._render_logcat())
        top.addWidget(self.logcat_filter, 1)
        layout.addLayout(top)

        self.logcat_view = QPlainTextEdit()
        self.logcat_view.setReadOnly(True)
        self.logcat_view.setMaximumBlockCount(20000)
        self.logcat_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.logcat_view.setPlaceholderText("Start logcat to stream device logs.")
        monospace(self.logcat_view)
        layout.addWidget(self.logcat_view, 1)

        self.logcat = None
        self._set_logcat_state("stopped")
        return page

    def _build_log_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 4, 8, 8)
        layout.setSpacing(4)
        row = QHBoxLayout()
        row.addWidget(QLabel("<b>Command log</b>"))
        row.addStretch(1)
        copy = QPushButton("Copy")
        copy.setIcon(self._icon("copy"))
        copy.clicked.connect(
            lambda: QApplication.clipboard().setText(self.log.toPlainText()))
        row.addWidget(copy)
        clear = QPushButton("Clear")
        clear.setIcon(self._icon("clear"))
        clear.clicked.connect(lambda: self.log.clear())
        row.addWidget(clear)
        layout.addLayout(row)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        self.log.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.log.setPlaceholderText("Every adb command and its output appears here.")
        monospace(self.log)
        layout.addWidget(self.log)
        return panel

    def _restore_settings(self):
        settings = QSettings()
        geometry = settings.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        for key, splitter in (("main_splitter_v2", self.main_split),
                              ("top_splitter_v2", self.top_split)):
            state = settings.value(key)
            if state is not None:
                splitter.restoreState(state)

    # ----------------------------------------------------------- task glue
    def log_line(self, text):
        self.log.appendPlainText(text)

    def run_async(self, args, serial=None, after=None, timeout=60,
                  log_cmd=True, log_output=True):
        self._task_seq += 1
        task_id = self._task_seq
        if log_cmd:
            self.log_line(f"$ {self.adb.display(args, serial)}")
        worker = Worker(task_id, lambda: self.adb.run(args, serial=serial, timeout=timeout))
        self._tasks[task_id] = _Task(after=after, log_output=log_output)
        self._workers[task_id] = worker
        worker.signals.done.connect(self._on_task_done)
        self.pool.start(worker)

    def run_bytes_async(self, args, serial=None, after=None, timeout=120):
        self._task_seq += 1
        task_id = self._task_seq
        self.log_line(f"$ {self.adb.display(args, serial)}")
        worker = Worker(task_id, lambda: self.adb.run_bytes(args, serial=serial, timeout=timeout))
        self._tasks[task_id] = _Task(after=after, log_output=False, binary=True)
        self._workers[task_id] = worker
        worker.signals.done.connect(self._on_task_done)
        self.pool.start(worker)

    @Slot(int, object)
    def _on_task_done(self, task_id, result):
        task = self._tasks.pop(task_id, None)
        self._workers.pop(task_id, None)
        if task is None:
            return
        if isinstance(result, Exception):
            self.log_line(f"[error] {result}")
            return
        if task.binary:
            _rc, _data, err, _shown = result
            if err.strip():
                self.log_line("[stderr] " + err.strip())
        elif task.log_output:
            if result.out.strip():
                self.log_line(result.out.rstrip())
            if result.err.strip():
                self.log_line("[stderr] " + result.err.strip())
            if result.rc != 0:
                self.log_line(f"[exit {result.rc}]")
        if task.after is not None:
            try:
                task.after(result)
            except Exception:  # noqa: BLE001 - never kill the event loop
                self.log_line("[ui error] " + traceback.format_exc().strip())

    def need_device(self):
        if not self.serial:
            QMessageBox.information(self, "No device", "Select a device first.")
            return False
        return True

    def _update_device_label(self):
        name = self.selected_device.display_name if self.selected_device else ""
        if self.serial and name:
            text = f"Device: {name}  \u00b7  {self.serial}"
        elif self.serial:
            text = f"Device: {self.serial}"
        else:
            text = "No device selected"
        if not self.adb.path:
            text += "  \u00b7  adb not found"
        self.device_label.setText(text)
        self.device_label.setToolTip(self.serial or "")

    # ------------------------------------------------------------ adb path
    def pick_adb(self):
        start = os.path.dirname(self.adb.path) if self.adb.path else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Select adb executable", start,
            "adb (adb.exe adb);;All files (*)",
        )
        if not path:
            return
        self.adb.path = path
        QSettings().setValue("adb_path", path)
        self.status.showMessage("adb: " + path)
        self._update_device_label()
        self.run_async(["version"], after=lambda _res: self.refresh_devices())

    def start_server(self):
        self.run_async(["start-server"], after=lambda _res: self.refresh_devices())

    def kill_server(self):
        if QMessageBox.question(
            self, "Kill adb server",
            "This disconnects every adb client. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        self.run_async(["kill-server"], after=lambda _res: self.refresh_devices())

    # ------------------------------------------------------------ devices
    def refresh_devices(self, quiet=False):
        if self._refreshing:
            return
        self._refreshing = True

        def after(result):
            self._refreshing = False
            if not result.ok and not result.out.strip():
                self.status.showMessage("adb unavailable: " + result.err.strip())
                if not quiet:
                    self.log_line("[adb] " + result.message)
                return
            devices = parse_devices(result.out)
            self._apply_devices(devices)
            if not devices:
                self.status.showMessage("No devices. Connect over USB or use Wireless.")

        self.run_async(["devices", "-l"], after=after,
                       log_cmd=not quiet, log_output=False)

    def _apply_devices(self, devices):
        signature = tuple((d.serial, d.state, d.model, d.transport) for d in devices)
        if signature == self._device_sig:
            return
        self._device_sig = signature
        previous = self.serial

        self.device_list.blockSignals(True)
        self.device_list.clear()
        for dev in devices:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, dev)
            item.setSizeHint(QSize(0, 56))
            tooltip = [f"serial: {dev.serial}", f"state: {dev.state}"]
            for name, value in (("model", dev.model), ("product", dev.product),
                                ("device", dev.device), ("usb", dev.transport)):
                if value:
                    tooltip.append(f"{name}: {value}")
            if dev.extra:
                tooltip.append(dev.extra)
            item.setToolTip("\n".join(tooltip))
            self.device_list.addItem(item)
            self.device_list.setItemWidget(item, DeviceRow(dev, self.device_list))
        self.device_list.blockSignals(False)

        target = None
        for row in range(self.device_list.count()):
            dev = self.device_list.item(row).data(Qt.ItemDataRole.UserRole)
            if dev.serial == previous:
                target = self.device_list.item(row)
                break

        if target is not None:
            self.device_list.setCurrentItem(target)
        elif previous:
            self.serial = None
            self.selected_device = None
            self._stop_logcat()
            self.clear_files()
            self._reset_apps()
            self.status.showMessage(f"{previous} disconnected")
            self.setWindowTitle(APP_NAME)

        if self.device_list.currentItem() is None and not self.serial:
            usable = [
                row for row in range(self.device_list.count())
                if self.device_list.item(row).data(Qt.ItemDataRole.UserRole).usable
            ]
            if len(usable) == 1:
                self.device_list.setCurrentRow(usable[0])

        self._sync_device_rows()
        self._update_device_label()

    def _sync_device_rows(self):
        current = self.device_list.currentItem()
        for row in range(self.device_list.count()):
            item = self.device_list.item(row)
            widget = self.device_list.itemWidget(item)
            if isinstance(widget, DeviceRow):
                widget.set_selected(item is current)

    def on_device_selected(self, item, _previous=None):
        self._sync_device_rows()
        if item is None:
            return
        dev = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(dev, Device):
            return

        if not dev.usable:
            self.serial = None
            self.selected_device = None
            self._stop_logcat()
            self.clear_files()
            self._reset_apps()
            hint = ("accept the USB debugging prompt on the phone"
                    if dev.state == "unauthorized" else "device is not ready")
            self.status.showMessage(f"{dev.serial}: {dev.state} - {hint}")
            self._update_device_label()
            self.setWindowTitle(APP_NAME)
            return

        changed = dev.serial != self.serial
        was_streaming = self._logcat_running()
        self.serial = dev.serial
        self.selected_device = dev
        if changed:
            self.cwd = "/sdcard"
            self.shell_cwd = "/sdcard"
            self._shell_update_prompt()
            self._reset_apps()
            if was_streaming:
                self._stop_logcat()
                QTimer.singleShot(0, self._start_logcat)

        self._update_device_label()
        self.setWindowTitle(f"{APP_NAME} - {dev.display_name or dev.serial}")
        self.goto(self.cwd)
        if self.tabs.currentWidget() is self.apps_tab:
            self.load_apps()

    def connect_device(self):
        addr = self.connect_edit.text().strip()
        if not addr:
            return
        if addr.lower().startswith("adb connect "):
            addr = addr[len("adb connect "):].strip()
        if ":" not in addr:
            addr += ":5555"

        def after(result):
            text = (result.out + " " + result.err).lower()
            if "connected to" in text or "already connected" in text:
                self.status.showMessage(f"Connected to {addr}")
                self.refresh_devices()
            else:
                QMessageBox.warning(self, "Connect", result.message)

        self.run_async(["connect", addr], after=after, timeout=30)

    def pair_device(self):
        addr = self.pair_addr.text().strip()
        code = self.pair_code.text().strip()
        if not addr or not code:
            QMessageBox.information(self, "Pair", "Enter the pairing ip:port and the code.")
            return

        def after(result):
            if result.ok and "paired" in result.out.lower():
                self.pair_code.clear()
                QMessageBox.information(
                    self, "Paired",
                    "Paired successfully.\n\nNow connect with the IP:port shown "
                    "under Wireless debugging (usually port 5555).",
                )
            else:
                QMessageBox.warning(self, "Pair", result.message)

        self.run_async(["pair", addr, code], after=after, timeout=60)

    def disconnect_device(self):
        item = self.device_list.currentItem()
        if item is None:
            return
        dev = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(dev, Device):
            return
        if ":" not in dev.serial:
            QMessageBox.information(
                self, "Disconnect",
                "Only wireless devices can be disconnected here. Unplug USB instead.",
            )
            return
        self.run_async(["disconnect", dev.serial],
                       after=lambda _res: self.refresh_devices())

    # -------------------------------------------------------------- files
    def clear_files(self):
        self.entries = []
        self.file_table.setRowCount(0)

    def goto(self, path):
        self.load_dir(normalize_remote(path))

    def goto_typed_path(self):
        self.goto(self.path_edit.text().strip() or "/")

    def go_up(self):
        if self.serial and self.cwd != "/":
            self.load_dir(remote_parent(self.cwd))

    def load_dir(self, path=None):
        if not self.serial:
            return
        target = normalize_remote(path if path is not None else self.cwd)
        self._dir_gen += 1
        generation = self._dir_gen
        serial = self.serial
        listing = target.rstrip("/") + "/"
        command = f"ls -la {shlex.quote(listing)}"

        def after(result):
            if generation != self._dir_gen or serial != self.serial:
                return  # a newer request or another device won
            if not result.ok and not result.out.strip():
                message = result.err.strip() or "Failed to list folder"
                self.status.showMessage(message)
                self.log_line(f"[ls] {message}")
                if target != self.cwd:
                    self.path_edit.setText(self.cwd)
                return
            self.cwd = target
            self.path_edit.setText(self.cwd)
            self.entries = parse_ls(result.out)
            self._populate_files()
            self.status.showMessage(
                f"{self.serial}  {self.cwd}  ({len(self.entries)} items)"
            )

        self.run_async(["shell", command], serial=serial, after=after,
                       timeout=120, log_output=False)

    def _populate_files(self):
        icon_dir = self.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon)
        icon_file = self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon)
        self.file_table.setRowCount(len(self.entries))
        for row, entry in enumerate(self.entries):
            name_item = QTableWidgetItem(entry.name + ("/" if entry.is_dir else ""))
            name_item.setIcon(icon_dir if entry.is_dir else icon_file)
            if entry.is_link and entry.link_target:
                name_item.setToolTip(f"symlink -> {entry.link_target}")
            self.file_table.setItem(row, 0, name_item)
            size_item = QTableWidgetItem("" if entry.is_dir else human_size(entry.size))
            size_item.setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.file_table.setItem(row, 1, size_item)
            modified_item = QTableWidgetItem(entry.modified)
            modified_item.setTextAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            self.file_table.setItem(row, 2, modified_item)

    def selected_entries(self):
        rows = sorted({index.row() for index in self.file_table.selectedIndexes()})
        return [self.entries[r] for r in rows if 0 <= r < len(self.entries)]

    def on_open(self, row, _column):
        if 0 <= row < len(self.entries) and self.entries[row].is_dir:
            self.goto(remote_join(self.cwd, self.entries[row].name))

    def _open_current(self):
        row = self.file_table.currentRow()
        if row >= 0:
            self.on_open(row, 0)

    def pull_selected(self):
        if not self.need_device():
            return
        selected = self.selected_entries()
        if not selected:
            QMessageBox.information(self, "Pull", "Select files or folders to pull.")
            return
        start = str(QSettings().value("last_pull_dir", "") or "")
        dest = QFileDialog.getExistingDirectory(self, "Save to folder", start)
        if not dest:
            return
        QSettings().setValue("last_pull_dir", dest)

        for entry in selected:
            def after(result):
                if not result.ok:
                    self.log_line(f"[pull] {result.message}")
            self.run_async(
                ["pull", remote_join(self.cwd, entry.name), dest],
                serial=self.serial, timeout=3600, log_output=False, after=after,
            )

    def push_files(self):
        if not self.need_device():
            return
        start = str(QSettings().value("last_push_dir", "") or "")
        files, _ = QFileDialog.getOpenFileNames(self, "Choose files to push", start)
        if files:
            QSettings().setValue("last_push_dir", os.path.dirname(files[0]))
            self.push_paths(files)

    def push_folder(self):
        if not self.need_device():
            return
        start = str(QSettings().value("last_push_dir", "") or "")
        folder = QFileDialog.getExistingDirectory(self, "Choose folder to push", start)
        if folder:
            QSettings().setValue("last_push_dir", os.path.dirname(folder))
            self.push_paths([folder])

    def push_paths(self, paths):
        if not self.need_device():
            return
        target = self.cwd.rstrip("/") + "/"
        for path in paths:
            def after(result, path=path):
                if result.ok:
                    self.status.showMessage(f"Pushed {os.path.basename(path)}")
                else:
                    self.log_line(f"[push] {result.message}")
                self.load_dir()
            self.run_async(["push", path, target], serial=self.serial,
                           timeout=3600, log_output=False, after=after)

    def new_folder(self):
        if not self.need_device():
            return
        name, ok = QInputDialog.getText(self, "New folder", "Folder name:")
        if ok and name.strip():
            target = remote_join(self.cwd, name.strip())
            self.run_async(["shell", f"mkdir -p {shlex.quote(target)}"],
                           serial=self.serial, after=lambda _res: self.load_dir())

    def rename_selected(self):
        if not self.need_device():
            return
        selected = self.selected_entries()
        if len(selected) != 1:
            QMessageBox.information(self, "Rename", "Select exactly one item.")
            return
        old = selected[0].name
        new, ok = QInputDialog.getText(self, "Rename", "New name:", text=old)
        if ok and new.strip() and new.strip() != old:
            self.run_async(
                ["shell", f"mv {shlex.quote(remote_join(self.cwd, old))} "
                          f"{shlex.quote(remote_join(self.cwd, new.strip()))}"],
                serial=self.serial, after=lambda _res: self.load_dir(),
            )

    def delete_selected(self):
        if not self.need_device():
            return
        selected = self.selected_entries()
        if not selected:
            return
        names = "\n".join(entry.name for entry in selected[:10])
        if len(selected) > 10:
            names += "\n..."
        answer = QMessageBox.question(
            self, "Delete",
            f"Permanently delete {len(selected)} item(s)?\n\n{names}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        targets = " ".join(
            shlex.quote(remote_join(self.cwd, entry.name)) for entry in selected)
        self.run_async(["shell", f"rm -rf {targets}"], serial=self.serial,
                       after=lambda _res: self.load_dir())

    def _files_menu(self, pos):
        item = self.file_table.itemAt(pos)
        if item is not None:
            self.file_table.selectRow(item.row())
        else:
            self.file_table.setCurrentCell(-1, -1)
        menu = QMenu(self)
        if self.file_table.currentRow() >= 0:
            menu.addAction("Open", self._open_current)
            menu.addAction("Pull to PC...", self.pull_selected)
            menu.addSeparator()
            menu.addAction("Rename...", self.rename_selected)
            menu.addAction("Delete", self.delete_selected)
            menu.addSeparator()
            menu.addAction("Copy device path", self.copy_device_path)
            menu.addAction("Open shell here", self.open_shell_here)
            menu.addSeparator()
        menu.addAction("New folder...", self.new_folder)
        menu.addAction("Push files here...", self.push_files)
        menu.exec(self.file_table.viewport().mapToGlobal(pos))

    def copy_device_path(self):
        selected = self.selected_entries()
        if not selected:
            return
        text = "\n".join(remote_join(self.cwd, entry.name) for entry in selected)
        QApplication.clipboard().setText(text)
        self.status.showMessage("Copied device path")

    def open_shell_here(self):
        self.shell_cwd = self.cwd
        self._shell_update_prompt()
        self.tabs.setCurrentWidget(self.shell_tab)
        self.shell_input.setFocus()

    def _focus_path(self):
        self.tabs.setCurrentWidget(self.files_tab)
        self.path_edit.setFocus()
        self.path_edit.selectAll()

    def _focus_shell(self):
        if self.tabs.currentWidget() is not self.shell_tab:
            self.tabs.setCurrentWidget(self.shell_tab)
        self.shell_input.setFocus()
        self.shell_input.selectAll()

    # -------------------------------------------------------------- shell
    def _shell_update_prompt(self):
        self.shell_prompt.setText(f"{self.shell_cwd} $")

    def _shell_append(self, text):
        self.shell_output.appendPlainText(text)

    def _use_shell_example(self, index):
        if index <= 0:
            return
        self.shell_input.setText(self.shell_examples.itemText(index))
        self.shell_examples.setCurrentIndex(0)
        self.shell_input.setFocus()

    def run_shell(self):
        command = self.shell_input.text().strip()
        if not command or not self.need_device():
            return
        self.shell_input.remember(command)
        self.shell_input.clear()

        if command in ("clear", "cls"):
            self.shell_output.clear()
            return

        if command == "cd" or command.startswith("cd "):
            target = command[2:].strip().strip("'\"") or "/sdcard"
            self._shell_cd(target)
            return

        full = f"cd {shlex.quote(self.shell_cwd)} 2>/dev/null; {command}"
        self._shell_append(f"{self.shell_cwd} $ {command}")

        def after(result):
            if result.out.strip():
                self._shell_append(result.out.rstrip())
            if result.err.strip():
                self._shell_append(result.err.rstrip())
            if result.rc != 0:
                self._shell_append(f"[exit {result.rc}]")
            self._shell_append("")

        self.run_async(["shell", full], serial=self.serial, after=after,
                       timeout=120, log_cmd=False, log_output=False)

    def _shell_cd(self, target):
        raw = target.strip().strip("'\"") or "/sdcard"
        if raw in ("~", "~/"):
            raw = "/sdcard"
        elif raw.startswith("~/"):
            raw = "/sdcard/" + raw[2:]
        # Leave relative paths (.., Download) to the device shell, resolved from
        # the current directory. pwd then gives us the absolute result.
        command = (f"cd {shlex.quote(self.shell_cwd)} && cd {shlex.quote(raw)} && pwd")

        def after(result):
            if result.ok:
                lines = [line for line in result.out.strip().splitlines() if line.strip()]
                actual = lines[-1].strip() if lines else self.shell_cwd
                self.shell_cwd = normalize_remote(actual)
                self._shell_update_prompt()
                self._shell_append(f"cd {raw}")
            else:
                self._shell_append(f"cd: {result.message}")

        self.run_async(["shell", command], serial=self.serial, after=after,
                       log_cmd=False, log_output=False)

    # --------------------------------------------------------------- apps
    def _reset_apps(self):
        self._apps_loaded = False
        self._apps_loading = False
        self._all_packages = []
        self.apps_table.setRowCount(0)
        self.apps_count.setText("0 packages")

    def load_apps(self):
        if not self.serial or self._apps_loading:
            return
        self._apps_loading = True
        serial = self.serial
        self.status.showMessage("Loading installed packages...")

        def after(result):
            if serial != self.serial:
                return
            self._apps_loading = False
            if not result.ok and not result.out.strip():
                self.status.showMessage("Could not list packages")
                self.log_line("[apps] " + result.message)
                return
            self._all_packages = parse_packages(result.out)
            self._apps_loaded = True
            self._populate_apps()
            self.status.showMessage(f"{len(self._all_packages)} packages", 4000)

        self.run_async(["shell", "pm list packages -f"], serial=serial, after=after,
                       timeout=180, log_cmd=False, log_output=False)

    def _populate_apps(self):
        needle = self.apps_filter.text().strip().lower()
        show_system = self.apps_system.isChecked()
        packages = [
            pkg for pkg in self._all_packages
            if (show_system or not pkg.system)
            and (not needle or needle in pkg.name.lower())
        ]
        self.apps_table.setRowCount(len(packages))
        for row, pkg in enumerate(packages):
            name_item = QTableWidgetItem(pkg.name)
            name_item.setData(Qt.ItemDataRole.UserRole, pkg)
            name_item.setToolTip(pkg.apk)
            self.apps_table.setItem(row, 0, name_item)
            self.apps_table.setItem(row, 1, QTableWidgetItem(
                "system" if pkg.system else "user"))
        self.apps_count.setText(f"{len(packages)} shown")

    def _selected_package(self):
        row = self.apps_table.currentRow()
        if row < 0:
            return None
        item = self.apps_table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _require_package(self):
        package = self._selected_package()
        if not isinstance(package, Package):
            QMessageBox.information(self, "Apps", "Select a package first.")
            return None
        return package

    def _apps_menu(self, pos):
        item = self.apps_table.itemAt(pos)
        if item is not None:
            self.apps_table.selectRow(item.row())
        else:
            self.apps_table.setCurrentCell(-1, -1)
            return
        if self.apps_table.currentRow() < 0:
            return
        menu = QMenu(self)
        menu.addAction("Launch", self.launch_app)
        menu.addAction("Force stop", self.force_stop_app)
        menu.addAction("App settings", self.open_app_settings)
        menu.addSeparator()
        menu.addAction("Clear data...", self.clear_app_data)
        menu.addAction("Uninstall...", self.uninstall_app)
        menu.exec(self.apps_table.viewport().mapToGlobal(pos))

    def install_apk(self):
        if not self.need_device():
            return
        start = str(QSettings().value("last_apk_dir", "") or "")
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose APK", start, "Android packages (*.apk);;All files (*)")
        if not path:
            return
        QSettings().setValue("last_apk_dir", os.path.dirname(path))
        self.status.showMessage("Installing " + os.path.basename(path) + "...")

        def after(result):
            if result.ok:
                self.status.showMessage("Installed " + os.path.basename(path))
                self.load_apps()
            else:
                self.status.showMessage("Install failed")
                text = (result.out + "\n" + result.err).strip()
                QMessageBox.warning(self, "Install APK", text or result.message)

        self.run_async(["install", "-r", path], serial=self.serial,
                       after=after, timeout=600)

    def uninstall_app(self):
        package = self._require_package()
        if not package:
            return
        answer = QMessageBox.question(
            self, "Uninstall", f"Uninstall {package.name}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        def after(result):
            if result.ok:
                self.status.showMessage("Uninstalled " + package.name)
                self.load_apps()
            else:
                QMessageBox.warning(self, "Uninstall", result.message)

        self.run_async(["uninstall", package.name], serial=self.serial, after=after)

    def clear_app_data(self):
        package = self._require_package()
        if not package:
            return
        answer = QMessageBox.question(
            self, "Clear data",
            f"Delete all app data for {package.name}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        def after(result):
            text = (result.out + " " + result.err).lower()
            if result.ok and "success" in text:
                self.status.showMessage("Cleared data for " + package.name)
            else:
                self.status.showMessage("pm clear failed")
                QMessageBox.warning(self, "Clear data", result.message)

        self.run_async(
            ["shell", f"pm clear {shlex.quote(package.name)}"],
            serial=self.serial, after=after,
        )

    def force_stop_app(self):
        package = self._require_package()
        if not package:
            return
        self.run_async(
            ["shell", f"am force-stop {shlex.quote(package.name)}"],
            serial=self.serial, log_output=False,
            after=lambda _res: self.status.showMessage("Stopped " + package.name),
        )

    def launch_app(self):
        package = self._require_package()
        if not package:
            return
        self.run_async(
            ["shell", f"monkey -p {shlex.quote(package.name)} "
                      f"-c android.intent.category.LAUNCHER 1"],
            serial=self.serial, log_output=False, timeout=30,
            after=lambda result: self.status.showMessage(
                "Launched " + package.name if result.ok else "Launch failed"),
        )

    def open_app_settings(self):
        package = self._require_package()
        if not package:
            return
        self.run_async(
            ["shell", "am start -a android.settings.APPLICATION_DETAILS_SETTINGS "
                      f"-d package:{shlex.quote(package.name)}"],
            serial=self.serial, log_output=False,
        )

    # ------------------------------------------------------------- logcat
    def _logcat_running(self):
        return self.logcat is not None and self.logcat.running()

    def _toggle_logcat(self):
        if self._logcat_running():
            self._stop_logcat()
        else:
            self._start_logcat()

    def _start_logcat(self):
        if self._logcat_running() or not self.need_device():
            return
        if not self.adb.path:
            QMessageBox.warning(self, "Logcat", "adb not found. Set the adb path first.")
            return
        self._logcat_lines.clear()
        self.logcat_view.clear()
        self.log_line("$ " + self.adb.display(["logcat", "-v", "time"], self.serial))

        stream = LogcatStream(self.adb.path, self.serial, self)
        stream.started.connect(lambda s=stream: self._on_logcat_started(s))
        stream.lines.connect(lambda lines, s=stream: self._on_logcat_lines(s, lines))
        stream.failed.connect(self._logcat_error)
        stream.finished.connect(lambda s=stream: self._on_logcat_finished(s))
        self.logcat = stream
        self._logcat_failed = False
        self._set_logcat_state("starting", "#9a6700" if not is_dark(self) else "#d29922",
                               "connecting to the device")
        stream.start()

    def _set_logcat_button(self, running):
        self.logcat_btn.setText("Stop" if running else "Start")
        self.logcat_btn.setIcon(self._icon("stop" if running else "play"))

    def _set_logcat_state(self, text, color=None, tooltip=""):
        if color is None:
            color = self.palette().color(QPalette.ColorRole.PlaceholderText).name()
        self.logcat_status.setText(text)
        self.logcat_status.setToolTip(tooltip or text)
        self.logcat_status.setStyleSheet(
            f"color: {color}; font-size: 11px; font-weight: 600;")

    def _on_logcat_started(self, stream):
        if self.logcat is not stream:
            return
        self._set_logcat_button(True)
        self._set_logcat_state(
            "streaming", "#1a7f37" if not is_dark(self) else "#3fb950",
            f"streaming from {self.serial}")

    def _on_logcat_lines(self, stream, lines):
        if self.logcat is stream:
            self._append_logcat_lines(lines)

    def _on_logcat_finished(self, stream):
        if self.logcat is not stream:
            return
        self.logcat = None
        self._set_logcat_button(False)
        if self._logcat_failed:
            self._set_logcat_state(
                "error", "#cf222e" if not is_dark(self) else "#f85149")
        else:
            self._set_logcat_state("stopped" if self.serial else "no device")

    def _stop_logcat(self):
        stream = self.logcat
        self.logcat = None
        if stream is not None:
            stream.stop()
        self._set_logcat_button(False)
        self._set_logcat_state("stopped" if self.serial else "no device")

    def _logcat_error(self, message):
        self._logcat_failed = True
        self.log_line("[logcat] " + message)
        self._set_logcat_state(
            "error", "#cf222e" if not is_dark(self) else "#f85149", message)
        self._set_logcat_button(False)

    def _append_logcat_lines(self, lines):
        needle = self.logcat_filter.text().strip().lower()
        bar = self.logcat_view.verticalScrollBar()
        old_value = bar.value()
        for line in lines:
            self._logcat_lines.append(line)
            if not needle or needle in line.lower():
                self.logcat_view.appendPlainText(line)
        if self.logcat_auto.isChecked():
            bar.setValue(bar.maximum())
        else:
            bar.setValue(old_value)

    def _render_logcat(self):
        needle = self.logcat_filter.text().strip().lower()
        bar = self.logcat_view.verticalScrollBar()
        old_value = bar.value()
        self.logcat_view.clear()
        for line in self._logcat_lines:
            if not needle or needle in line.lower():
                self.logcat_view.appendPlainText(line)
        if self.logcat_auto.isChecked():
            bar.setValue(bar.maximum())
        else:
            bar.setValue(old_value)

    def clear_logcat(self):
        self._logcat_lines.clear()
        self.logcat_view.clear()

    def save_logcat(self):
        default = os.path.join(os.path.expanduser("~"), "logcat.log")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save logcat", default, "Log files (*.log *.txt);;All files (*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", errors="replace") as handle:
                handle.write(self.logcat_view.toPlainText())
        except OSError as exc:
            QMessageBox.warning(self, "Save failed", str(exc))

    # --------------------------------------------------- device extras
    def take_screenshot(self):
        if not self.need_device():
            return
        self.status.showMessage("Capturing screenshot...")

        def after(result):
            rc, data, err, _shown = result
            if rc != 0 or not data:
                message = err.strip() or "Could not capture the screen."
                self.log_line("[screenshot] " + message)
                QMessageBox.warning(self, "Screenshot", message)
                return
            self.status.showMessage("Screenshot captured")
            ScreenshotDialog(data, self.serial, self).exec()

        self.run_bytes_async(["exec-out", "screencap", "-p"], serial=self.serial,
                             after=after)

    def show_device_info(self):
        if not self.need_device():
            return
        self.status.showMessage("Reading device info...")

        def after(result):
            if not result.ok and not result.out.strip():
                QMessageBox.warning(self, "Device info", result.message)
                return
            info = {}
            key = None
            for line in result.out.splitlines():
                line = line.strip()
                if line.startswith("@@"):
                    key = line[2:].strip()
                    info[key] = []
                elif key:
                    info[key].append(line)
            info = {k: " ".join(v).strip() for k, v in info.items()}
            self.status.showMessage("Device info")
            DeviceInfoDialog(self.serial, info, self).exec()

        self.run_async(["shell", INFO_SCRIPT], serial=self.serial,
                       after=after, timeout=120, log_cmd=False, log_output=False)

    def reboot(self, mode):
        if not self.need_device():
            return
        label = mode or "system"
        answer = QMessageBox.question(
            self, "Reboot", f"Reboot the device into {label}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.run_async(["reboot"] + ([mode] if mode else []), serial=self.serial,
                       after=lambda _res: self.refresh_devices())

    # ------------------------------------------------------------- misc
    def _on_tab_changed(self, _index):
        if (self.tabs.currentWidget() is self.apps_tab
                and self.serial and not self._apps_loaded and not self._apps_loading):
            self.load_apps()

    def show_about(self):
        QMessageBox.about(
            self, f"About {APP_NAME}",
            f"<h3>{APP_NAME} {__version__}</h3>"
            "<p>A PySide6 front end for Android platform-tools.</p>"
            f"<p>PySide6 {PYSIDE_VERSION}<br>adb: {self.adb.path or 'not found'}</p>"
            "<p>Use at your own risk. Delete, push, clear-data and uninstall "
            "actions change your device.</p>",
        )

    def closeEvent(self, event):
        self._auto_timer.stop()
        self._stop_logcat()
        settings = QSettings()
        settings.setValue("geometry", self.saveGeometry())
        settings.setValue("main_splitter_v2", self.main_split.saveState())
        settings.setValue("top_splitter_v2", self.top_split.saveState())
        settings.setValue("adb_path", self.adb.path)
        super().closeEvent(event)


INFO_SCRIPT = r"""
echo @@model
getprop ro.product.model
echo @@brand
getprop ro.product.brand
echo @@android
getprop ro.build.version.release
echo @@sdk
getprop ro.build.version.sdk
echo @@abi
getprop ro.product.cpu.abi
echo @@serialno
getprop ro.serialno
echo @@screen
wm size
echo @@density
wm density
echo @@battery
dumpsys battery | grep -E 'level|status|temperature'
echo @@storage
df -h /data | tail -n 1
echo @@wifi
ip -4 addr show wlan0 2>/dev/null | grep -o 'inet [0-9.]*'
"""


def main():
    if "--version" in sys.argv:
        print(f"{APP_NAME} {__version__}")
        return
    app = QApplication(sys.argv)
    app.setApplicationName("adb-gui")
    app.setOrganizationName("adb-gui")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
