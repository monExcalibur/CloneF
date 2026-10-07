from __future__ import annotations

import sys
import threading

from PyQt5.QtCore import QObject, QTimer, Qt, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QLabel, QPlainTextEdit,
    QPushButton, QVBoxLayout, QWidget,
)

from .config import Config
from .answers import looks_like_code_answer
from .gemini import GeminiClient
from .linux_input import LinuxKeyboard
from .screenshots import ScreenCapture
from .typing_state import ManualTyper


class Signals(QObject):
    action = pyqtSignal(str)
    answer = pyqtSignal(str)
    error = pyqtSignal(str)


class Overlay(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.label = QLabel(self)
        self.label.setTextFormat(Qt.PlainText)
        self.label.setWordWrap(True)
        self.label.setFont(QFont("monospace", 11))
        self.label.setStyleSheet("color: #cccccc; background-color: rgba(0, 0, 0, 150); padding: 8px;")
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.hide)

    def display(self, text: str):
        screen = QApplication.primaryScreen().availableGeometry()
        width, height = min(500, screen.width() - 30), min(260, screen.height() - 30)
        self.setGeometry(screen.left() + 15, screen.bottom() - height - 30, width, height)
        self.label.setGeometry(0, 0, width, height)
        self.label.setText(text)
        self.show()
        self.timer.start(6000)


class AssistantWindow(QWidget):
    def __init__(self, config: Config, client=None, keyboard_factory=LinuxKeyboard):
        super().__init__()
        self.config = config
        self.client = client if client is not None else GeminiClient(config)
        self.keyboard_factory = keyboard_factory
        self.keyboard = None
        self.signals = Signals(self)
        self.signals.action.connect(self._on_action)
        self.signals.answer.connect(self._on_answer)
        self.signals.error.connect(self._on_error)
        self.typer = ManualTyper()
        self.materials = ""
        self.material_type = ""
        self.processing = False
        self.closed = False
        self.start_pending = False
        self.step_pending = False
        self.restore_controls = False
        self.last_answer = ""
        self.overlay = Overlay()
        self.capture = ScreenCapture(self)
        self.capture.captured.connect(self._on_capture)
        self.capture.failed.connect(self._on_error)
        self.setWindowTitle("CloneF — ассистент")
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.resize(650, 450)
        layout = QVBoxLayout(self)
        self.input_status = QLabel("Проверка клавиатуры…")
        self.input_status.setWordWrap(True)
        self.input_status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.input_status)
        actions = QHBoxLayout()
        for title, callback in (
            ("Снять экран", self.request_screenshot),
            ("Открыть изображение", self.open_image),
            ("Лекции БД", lambda: self.load_materials("DB")),
            ("Лекции Java", lambda: self.load_materials("Java")),
        ):
            button = QPushButton(title)
            button.clicked.connect(callback)
            actions.addWidget(button)
        layout.addLayout(actions)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        layout.addWidget(self.output)
        controls = QHBoxLayout()
        self.copy_button = QPushButton("Копировать ответ")
        self.copy_button.clicked.connect(self.copy_answer)
        controls.addWidget(self.copy_button)
        self.type_button = QPushButton("Начать ввод через 3 секунды")
        self.type_button.clicked.connect(self.start_typing_delayed)
        self.type_button.setEnabled(False)
        controls.addWidget(self.type_button)
        pause = QPushButton("Пауза")
        pause.clicked.connect(self.pause_typing)
        controls.addWidget(pause)
        layout.addLayout(controls)
        self.status = QLabel("Задайте GEMINI_API_KEY в .env" if not config.api_keys else "Готово")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.status)
        self.send_timer = QTimer(self)
        self.send_timer.setInterval(100)
        self.send_timer.timeout.connect(self._prepare_step)
        self.send_timer.start()
        QTimer.singleShot(0, self._start_keyboard)

    def _status(self, text, overlay=False):
        self.status.setText(text)
        if overlay and not self.closed:
            self.overlay.display(text)

    def _start_keyboard(self):
        if self.closed:
            return
        if sys.platform != "linux":
            self.input_status.setText("Глобальные клавиши и ручной ввод доступны в Linux. Кнопки и копирование работают.")
            return
        try:
            self.keyboard = self.keyboard_factory(self.signals.action.emit, self.config.paste_shortcut)
            self.keyboard.start()
            details = "Горячие клавиши: Right Shift+P — экран, Right Shift+I — ввод, Ctrl+Shift+D/J — лекции, Ctrl+Shift+Q — выход."
            if self.keyboard.typing_available:
                self.type_button.setEnabled(True)
            else:
                details += "\n" + self.keyboard.typing_error
            self.input_status.setText(details)
        except Exception as error:
            if self.keyboard:
                self.keyboard.close()
                self.keyboard = None
            self.input_status.setText(f"Клавиатура недоступна: {error}\nРаботают кнопки и копирование ответа.")

    def load_materials(self, kind: str):
        if self.processing:
            self._status("Дождитесь текущего ответа перед сменой лекций")
            return
        try:
            path = self.config.material_path(kind)
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            self._on_error(f"Чтение лекций {kind}: {error}")
            return
        self.pause_typing()
        self.typer.load("")
        self.materials = content
        self.material_type = kind
        self._status(f"Загружены лекции {kind}: {len(content)} символов", overlay=True)

    def _begin_request(self) -> bool:
        if self.processing:
            self._status("Запрос уже выполняется", overlay=True)
            return False
        if not self.config.api_keys:
            self._on_error("Не задан GEMINI_API_KEY. Заполните .env и перезапустите приложение.")
            return False
        self.pause_typing()
        self.processing = True
        self._status("Подготовка изображения…")
        return True

    def request_screenshot(self):
        if not self._begin_request():
            return
        self.restore_controls = self.isVisible()
        self.hide()
        self.overlay.hide()
        QTimer.singleShot(250, lambda: self.capture.capture() if not self.closed else None)

    def open_image(self):
        if self.processing:
            self._status("Запрос уже выполняется")
            return
        self.pause_typing()
        path, _ = QFileDialog.getOpenFileName(self, "Выберите изображение", "", "Изображения (*.png *.jpg *.jpeg *.webp *.bmp)")
        if not path or not self._begin_request():
            return
        try:
            from pathlib import Path
            self._on_capture(Path(path).read_bytes())
        except OSError as error:
            self._on_error(f"Чтение изображения: {error}")

    def _restore_window(self):
        if self.restore_controls and not self.closed:
            self.show()
        self.restore_controls = False

    def _on_capture(self, image_bytes):
        self._restore_window()
        if self.closed:
            return
        self._status("Gemini обрабатывает изображение…", overlay=True)
        materials, kind = self.materials, self.material_type

        def work():
            try:
                answer = self.client.ask(image_bytes, materials, kind)
            except Exception as error:
                if not self.closed:
                    self.signals.error.emit(f"Обработка изображения: {error}")
            else:
                if not self.closed:
                    self.signals.answer.emit(answer)

        threading.Thread(target=work, name="clonef-gemini", daemon=True).start()

    def _on_answer(self, answer):
        if self.closed:
            return
        self.processing = False
        self.pause_typing()
        self.last_answer = answer
        self.output.setPlainText(answer)
        self.typer.load(answer)
        self._status(f"Ответ готов: {len(answer)} символов")
        self.overlay.display("Код готов. Right Shift+I — ввод." if looks_like_code_answer(answer) else answer)

    def _on_error(self, error):
        if self.closed:
            return
        self.processing = False
        self._restore_window()
        self.pause_typing()
        self._status(error, overlay=True)

    def copy_answer(self):
        if self.last_answer:
            QApplication.clipboard().setText(self.last_answer)
            self._status("Ответ скопирован")

    def start_typing_delayed(self):
        if self.start_pending or self.typer.active:
            return
        if not self.typer.text or self.typer.index == len(self.typer.text):
            self._status("Нет текста для ввода. Новый ответ загружает новый буфер.")
            return
        self.start_pending = True
        self._status("Переключитесь в нужное окно: ввод включится через 3 секунды", overlay=True)
        generation = self.typer.generation

        def start():
            if self.start_pending and not self.closed and self.typer.generation == generation:
                self.start_pending = False
                self._resume_typing()

        QTimer.singleShot(3000, start)

    def _resume_typing(self):
        if self.processing:
            self._status("Дождитесь текущего ответа перед включением ввода", overlay=True)
            return
        if not self.keyboard or not self.keyboard.typing_available:
            self._status("Ручной ввод недоступен. Используйте копирование ответа.", overlay=True)
            return
        if not self.typer.resume():
            self._status("Буфер пуст или уже введён", overlay=True)
            return
        try:
            self.keyboard.set_typing(True)
        except Exception as error:
            self.typer.pause()
            self._status(f"Включение ввода: {error}", overlay=True)
            return
        self._status("Отпустите клавиши, затем нажимайте любые клавиши для ввода. Esc — пауза.", overlay=True)

    def pause_typing(self):
        self.start_pending = False
        self.typer.pause()
        if self.keyboard:
            self.keyboard.set_typing(False)
        self._status(f"Пауза: {self.typer.index}/{len(self.typer.text)}")

    def _prepare_step(self):
        if self.step_pending:
            return
        step = self.typer.next_step()
        if step is None:
            return
        self.step_pending = True
        if step.character not in ("\n", "\t"):
            QApplication.clipboard().setText(step.character)

        def send():
            self.step_pending = False
            if self.closed or not self.typer.accepts(step):
                return
            try:
                self.keyboard.send_character(step.character)
                self.typer.complete(step)
            except Exception as error:
                self.pause_typing()
                self._status(f"Ввод остановлен: {error}", overlay=True)
                return
            self._status(f"Ввод: {self.typer.index}/{len(self.typer.text)}")
            if not self.typer.active:
                self.keyboard.set_typing(False)
                self._status("Ввод завершён", overlay=True)

        QTimer.singleShot(40, send)

    def _on_action(self, action):
        if self.closed:
            return
        if action == "step":
            self.typer.request_step()
        elif action == "ready":
            if self.typer.active:
                self._status("Режим ввода включён. Esc — пауза.", overlay=True)
        elif action == "pause":
            self.pause_typing()
        elif action == "toggle":
            self.pause_typing() if self.typer.active else self._resume_typing()
        elif action == "screenshot":
            self.request_screenshot()
        elif action in ("DB", "Java"):
            self.load_materials(action)
        elif action == "quit":
            self.close()
        elif action.startswith("error:"):
            self.pause_typing()
            self.type_button.setEnabled(False)
            self.input_status.setText(action[6:])

    def closeEvent(self, event):
        self.closed = True
        self.send_timer.stop()
        self.typer.pause()
        self.capture.cancel()
        if self.keyboard:
            self.keyboard.close()
        self.overlay.close()
        event.accept()
