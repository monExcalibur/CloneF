from __future__ import annotations

import logging
import selectors
import threading


log = logging.getLogger(__name__)


class InputUnavailable(RuntimeError):
    pass


class LinuxKeyboard:
    def __init__(self, on_action, paste_shortcut="ctrl+v", evdev_module=None):
        if evdev_module is None:
            try:
                import evdev as evdev_module
            except ImportError as error:
                raise InputUnavailable("Не установлен evdev: установите requirements.txt") from error
        self.evdev = evdev_module
        self.e = evdev_module.ecodes
        self.on_action = on_action
        self.paste_shortcut = paste_shortcut
        self.devices = []
        self.virtual = None
        self.typing_available = False
        self.typing_error = ""
        self._selector = selectors.DefaultSelector()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._pressed = set()
        self._grabbed = []
        self._arming = False
        self._active = False
        self._closed = False

    def start(self):
        try:
            for path in self.evdev.list_devices():
                device = self.evdev.InputDevice(path)
                keys = set(device.capabilities().get(self.e.EV_KEY, []))
                if device.name.startswith("CloneF ") or not {self.e.KEY_A, self.e.KEY_Z, self.e.KEY_ENTER}.issubset(keys):
                    device.close()
                    continue
                self.devices.append(device)
                self._selector.register(device, selectors.EVENT_READ)
                self._pressed.update((device.path, code) for code in device.active_keys())
            if not self.devices:
                raise InputUnavailable("Нет доступных клавиатур в /dev/input. Настройте права по README.")
            try:
                self.virtual = self.evdev.UInput({self.e.EV_KEY: list(range(1, 249))}, name="CloneF virtual keyboard")
                self.typing_available = True
            except OSError as error:
                self.typing_error = f"Нет доступа к /dev/uinput: {error.strerror or type(error).__name__}. См. README."
            self._thread = threading.Thread(target=self._run, name="clonef-keyboard", daemon=True)
            self._thread.start()
        except Exception:
            self.close()
            raise

    def set_typing(self, active: bool):
        with self._lock:
            if active:
                if not self.typing_available or self._closed or self._stop.is_set():
                    raise InputUnavailable(self.typing_error or "Клавиатурный ввод недоступен")
                if not self._active:
                    self._arming = True
            else:
                self._arming = False
                self._release_locked()

    def _arm_if_released(self):
        with self._lock:
            if not self._arming:
                return
            if any(device.active_keys() for device in self.devices):
                return
            try:
                for device in self.devices:
                    device.grab()
                    self._grabbed.append(device)
                if any(device.active_keys() for device in self.devices):
                    self._release_locked()
                    return
            except OSError as error:
                self._arming = False
                self._release_locked()
                self.on_action("error:" + f"Не удалось перехватить клавиатуру: {error}")
                return
            self._arming = False
            self._active = True
            self._pressed.clear()
            self.on_action("ready")

    def _release_locked(self):
        self._active = False
        grabbed, self._grabbed = self._grabbed, []
        for device in reversed(grabbed):
            try:
                device.ungrab()
            except OSError as error:
                log.warning("Освобождение клавиатуры %s: %s", device.path, error)
                device.close()
                self._stop.set()
                self.typing_available = False

    def _handle_key(self, device, event):
        with self._lock:
            pair = (device.path, event.code)
            if event.value == 0:
                self._pressed.discard(pair)
                return
            if event.value != 1:
                return
            self._pressed.add(pair)
            held = {code for _, code in self._pressed}
            ctrl = bool(held & {self.e.KEY_LEFTCTRL, self.e.KEY_RIGHTCTRL})
            shift = bool(held & {self.e.KEY_LEFTSHIFT, self.e.KEY_RIGHTSHIFT})
            right_shift = self.e.KEY_RIGHTSHIFT in held
            action = None
            if ctrl and shift and event.code == self.e.KEY_Q:
                action = "quit"
            elif right_shift and event.code == self.e.KEY_P:
                action = "screenshot"
            elif right_shift and event.code == self.e.KEY_I:
                action = "pause" if self._active or self._arming else "toggle"
            elif ctrl and shift and event.code == self.e.KEY_D:
                action = "DB"
            elif ctrl and shift and event.code == self.e.KEY_J:
                action = "Java"
            elif self._active and event.code == self.e.KEY_ESC:
                action = "pause"
            if action:
                if self._active or self._arming:
                    self._arming = False
                    self._release_locked()
                self.on_action(action)
                return
            modifiers = {
                self.e.KEY_LEFTCTRL, self.e.KEY_RIGHTCTRL,
                self.e.KEY_LEFTSHIFT, self.e.KEY_RIGHTSHIFT,
                self.e.KEY_LEFTALT, self.e.KEY_RIGHTALT,
                self.e.KEY_LEFTMETA, self.e.KEY_RIGHTMETA,
                self.e.KEY_CAPSLOCK, self.e.KEY_NUMLOCK,
            }
            if self._active and event.code not in modifiers:
                self.on_action("step")

    def _run(self):
        try:
            while not self._stop.is_set():
                for key, _ in self._selector.select(timeout=0.05):
                    device = key.fileobj
                    for event in device.read():
                        if event.type == self.e.EV_SYN and event.code == self.e.SYN_DROPPED:
                            raise InputUnavailable("Потеряны события клавиатуры. Перезапустите приложение.")
                        if event.type == self.e.EV_KEY:
                            self._handle_key(device, event)
                self._arm_if_released()
        except Exception as error:
            with self._lock:
                self._stop.set()
                self._arming = False
                self._release_locked()
            self.on_action("error:" + f"Чтение клавиатуры остановлено: {error}")
        finally:
            with self._lock:
                self._release_locked()

    def send_character(self, character: str):
        with self._lock:
            if not self._active or not self.virtual:
                raise InputUnavailable("Перехват клавиатуры не активен")
            if character == "\n":
                keys = [self.e.KEY_ENTER]
            elif character == "\t":
                keys = [self.e.KEY_TAB]
            else:
                keys = [self.e.KEY_LEFTCTRL]
                if self.paste_shortcut == "ctrl+shift+v":
                    keys.append(self.e.KEY_LEFTSHIFT)
                keys.append(self.e.KEY_V)
            pressed = []
            failure = None
            try:
                for code in keys:
                    self.virtual.write(self.e.EV_KEY, code, 1)
                    pressed.append(code)
                self.virtual.syn()
            except OSError as error:
                failure = error
            finally:
                for code in reversed(pressed):
                    try:
                        self.virtual.write(self.e.EV_KEY, code, 0)
                    except OSError as error:
                        failure = failure or error
                try:
                    self.virtual.syn()
                except OSError as error:
                    failure = failure or error
            if failure:
                self.virtual.close()
                self.virtual = None
                self.typing_available = False
                self._release_locked()
                raise InputUnavailable(f"Отправка виртуальных клавиш: {failure}") from failure

    def close(self):
        with self._lock:
            self._closed = True
            self._stop.set()
            self._arming = False
            self._release_locked()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=1)
        with self._lock:
            for device in self.devices:
                device.close()
            self.devices.clear()
            self._selector.close()
            if self.virtual:
                self.virtual.close()
                self.virtual = None
            self.typing_available = False
