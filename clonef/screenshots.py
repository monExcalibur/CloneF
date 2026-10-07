from __future__ import annotations

import io
import os
import threading
import uuid
from pathlib import Path

from PIL import ImageGrab
from PyQt5.QtCore import QObject, QTimer, QUrl, pyqtSignal, pyqtSlot


class PortalResponse(QObject):
    def __init__(self, capture, generation):
        super().__init__(capture)
        self.capture = capture
        self.generation = generation

    @pyqtSlot("uint", "QVariantMap")
    def receive(self, code, results):
        self.capture._portal_response(code, results, self.generation)


class ScreenCapture(QObject):
    captured = pyqtSignal(bytes)
    failed = pyqtSignal(str)
    _finished = pyqtSignal(bytes, str, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.bus = None
        self.request_path = ""
        self.busy = False
        self.watcher = None
        self.receiver = None
        self.generation = 0
        self.timeout = QTimer(self)
        self.timeout.setSingleShot(True)
        self.timeout.timeout.connect(self._timed_out)
        self._finished.connect(self._on_finished)

    def capture(self):
        if self.busy:
            return
        self.busy = True
        self.generation += 1
        if os.environ.get("XDG_SESSION_TYPE") == "wayland" or os.environ.get("WAYLAND_DISPLAY"):
            self._portal_capture(self.generation)
        else:
            threading.Thread(target=self._grab, args=(self.generation,), name="clonef-screenshot", daemon=True).start()

    def _grab(self, generation):
        try:
            screenshot = ImageGrab.grab()
            output = io.BytesIO()
            screenshot.save(output, format="PNG")
            self._finished.emit(output.getvalue(), "", generation)
        except Exception as error:
            self._finished.emit(b"", f"Захват экрана: {error}", generation)

    def _portal_capture(self, generation):
        try:
            from PyQt5.QtDBus import QDBusConnection, QDBusMessage, QDBusPendingCallWatcher, QDBusVariant
            self.bus = QDBusConnection.sessionBus()
            if not self.bus.isConnected():
                raise RuntimeError("Нет соединения с пользовательским D-Bus")
            token = "clonef_" + uuid.uuid4().hex
            sender = self.bus.baseService().lstrip(":").replace(".", "_")
            self.request_path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
            self.receiver = PortalResponse(self, generation)
            if not self.bus.connect("org.freedesktop.portal.Desktop", self.request_path, "org.freedesktop.portal.Request", "Response", self.receiver.receive):
                raise RuntimeError("Не удалось подписаться на ответ портала")
            message = QDBusMessage.createMethodCall("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop", "org.freedesktop.portal.Screenshot", "Screenshot")
            message.setArguments(["", {"handle_token": QDBusVariant(token), "interactive": QDBusVariant(False)}])
            self.watcher = QDBusPendingCallWatcher(self.bus.asyncCall(message), self)
            self.watcher.finished.connect(lambda watcher: self._portal_started(watcher, generation))
            self.timeout.start(60000)
        except Exception as error:
            self._on_finished(b"", f"Захват экрана через портал: {error}", generation)

    def _portal_started(self, watcher, generation):
        if generation != self.generation:
            watcher.deleteLater()
            return
        from PyQt5.QtDBus import QDBusPendingReply
        reply = QDBusPendingReply(watcher)
        if reply.isError() and self.busy:
            self._on_finished(b"", f"Портал Screenshot: {reply.error().message()}", generation)
        watcher.deleteLater()
        self.watcher = None

    def _portal_response(self, code, results, generation=None):
        generation = self.generation if generation is None else generation
        if not self.busy or generation != self.generation:
            return
        if code != 0:
            self._on_finished(b"", "Захват экрана отменён" if code == 1 else "Портал не смог снять экран", generation)
            return
        try:
            uri = results.get("uri", "")
            if hasattr(uri, "variant"):
                uri = uri.variant()
            path = QUrl(str(uri)).toLocalFile()
            if not path:
                raise ValueError("Портал не вернул локальный файл изображения")
            self._on_finished(Path(path).read_bytes(), "", generation)
        except Exception as error:
            self._on_finished(b"", f"Чтение скриншота портала: {error}", generation)

    def _timed_out(self):
        self.cancel()
        self.failed.emit("Портал не ответил за 60 секунд. Проверьте диалог разрешения захвата экрана.")

    def cancel(self):
        if self.bus and self.request_path:
            from PyQt5.QtDBus import QDBusMessage
            message = QDBusMessage.createMethodCall("org.freedesktop.portal.Desktop", self.request_path, "org.freedesktop.portal.Request", "Close")
            self.bus.asyncCall(message)
        self._disconnect()
        self.busy = False
        self.generation += 1

    def _disconnect(self):
        self.timeout.stop()
        if self.bus and self.request_path and self.receiver:
            self.bus.disconnect("org.freedesktop.portal.Desktop", self.request_path, "org.freedesktop.portal.Request", "Response", self.receiver.receive)
        if self.receiver:
            self.receiver.deleteLater()
            self.receiver = None
        self.request_path = ""

    @pyqtSlot(bytes, str, int)
    def _on_finished(self, data, error, generation):
        if not self.busy or generation != self.generation:
            return
        self._disconnect()
        self.busy = False
        if error:
            self.failed.emit(error)
        else:
            self.captured.emit(data)
