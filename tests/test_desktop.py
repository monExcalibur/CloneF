from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QUrl
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication

from clonef.config import Config
from clonef.desktop import AssistantWindow
from clonef.screenshots import ScreenCapture


class FakeKeyboard:
    def __init__(self, callback, shortcut):
        self.callback = callback
        self.typing_available = True
        self.typing_error = ""
        self.active = False
        self.sent = []
        self.closed = False

    def start(self):
        pass

    def set_typing(self, active):
        self.active = active

    def send_character(self, character):
        if not self.active:
            raise RuntimeError("inactive")
        self.sent.append(character)

    def close(self):
        self.active = False
        self.closed = True


class DesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def make_window(self, keys=("dummy",), client=None):
        with patch("clonef.desktop.sys.platform", "linux"):
            window = AssistantWindow(Config(api_keys=keys), client=client or Mock(), keyboard_factory=FakeKeyboard)
            self.app.processEvents()
        window.send_timer.stop()
        self.addCleanup(window.close)
        return window

    def test_answer_copy_and_input_end(self):
        window = self.make_window()
        window._on_answer("Я")
        self.assertEqual(window.output.toPlainText(), "Я")
        window.copy_answer()
        self.assertEqual(self.app.clipboard().text(), "Я")
        window._resume_typing()
        window._on_action("step")
        window._prepare_step()
        QTest.qWait(80)
        self.assertEqual(window.keyboard.sent, ["Я"])
        self.assertEqual(window.typer.index, 1)
        self.assertFalse(window.keyboard.active)

    def test_pause_cancels_scheduled_send_without_skipping(self):
        window = self.make_window()
        window._on_answer("AB")
        window._resume_typing()
        window._on_action("step")
        window._prepare_step()
        window.pause_typing()
        QTest.qWait(80)
        self.assertEqual(window.keyboard.sent, [])
        self.assertEqual(window.typer.index, 0)

    def test_new_answer_cancels_old_scheduled_character(self):
        window = self.make_window()
        window._on_answer("OLD")
        window._resume_typing()
        window._on_action("step")
        window._prepare_step()
        window._on_answer("NEW")
        QTest.qWait(80)
        self.assertEqual(window.keyboard.sent, [])
        self.assertEqual(window.typer.text, "NEW")
        self.assertFalse(window.keyboard.active)

    def test_missing_key_never_starts_capture(self):
        window = self.make_window(keys=())
        window.capture.capture = Mock()
        window.request_screenshot()
        QTest.qWait(280)
        window.capture.capture.assert_not_called()
        self.assertIn("GEMINI_API_KEY", window.status.text())

    def test_worker_result_returns_to_gui(self):
        client = Mock()
        client.ask.return_value = "1:T"
        window = self.make_window(client=client)
        self.assertTrue(window._begin_request())
        window._on_capture(b"test-image")
        QTest.qWait(100)
        self.assertEqual(window.output.toPlainText(), "1:T")
        self.assertFalse(window.processing)
        self.assertEqual(window.overlay.label.text(), "1:T")
        client.ask.assert_called_once_with(b"test-image", "", "")

    def test_close_releases_backend(self):
        window = self.make_window()
        window._on_answer("A")
        window._resume_typing()
        window.close()
        self.assertTrue(window.keyboard.closed)
        self.assertFalse(window.keyboard.active)

    def test_portal_response_reads_only_local_file(self):
        capture = ScreenCapture()
        self.addCleanup(capture.cancel)
        captured = []
        capture.captured.connect(captured.append)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.png"
            path.write_bytes(b"image-data")
            capture.busy = True
            capture._portal_response(0, {"uri": QUrl.fromLocalFile(str(path)).toString()})
        self.assertEqual(captured, [b"image-data"])
        failed = []
        capture.failed.connect(failed.append)
        capture.busy = True
        capture._portal_response(0, {"uri": "https://example.com/image"})
        self.assertIn("локальный файл", failed[-1])

    def test_portal_cancel_is_explicit_failure(self):
        capture = ScreenCapture()
        self.addCleanup(capture.cancel)
        failed = []
        capture.failed.connect(failed.append)
        capture.busy = True
        capture._portal_response(1, {})
        self.assertFalse(capture.busy)
        self.assertEqual(failed, ["Захват экрана отменён"])

    def test_late_capture_result_does_not_finish_new_request(self):
        capture = ScreenCapture()
        self.addCleanup(capture.cancel)
        captured = []
        capture.captured.connect(captured.append)
        capture.busy = True
        previous = capture.generation
        capture.cancel()
        capture.busy = True
        capture._finished.emit(b"old", "", previous)
        self.assertTrue(capture.busy)
        self.assertEqual(captured, [])
        capture._finished.emit(b"new", "", capture.generation)
        self.assertEqual(captured, [b"new"])

    def test_late_portal_cancel_does_not_cancel_new_request(self):
        capture = ScreenCapture()
        self.addCleanup(capture.cancel)
        failed = []
        capture.failed.connect(failed.append)
        previous = capture.generation
        capture.cancel()
        capture.busy = True
        capture._portal_response(1, {}, previous)
        self.assertTrue(capture.busy)
        self.assertEqual(failed, [])
