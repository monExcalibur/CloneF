from __future__ import annotations

import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from clonef.linux_input import InputUnavailable, LinuxKeyboard


NAMES = "KEY_A KEY_Z KEY_ENTER KEY_Q KEY_P KEY_I KEY_D KEY_J KEY_ESC KEY_TAB KEY_V KEY_LEFTCTRL KEY_RIGHTCTRL KEY_LEFTSHIFT KEY_RIGHTSHIFT KEY_LEFTALT KEY_RIGHTALT KEY_LEFTMETA KEY_RIGHTMETA KEY_CAPSLOCK KEY_NUMLOCK".split()
E = SimpleNamespace(**{name: index + 1 for index, name in enumerate(NAMES)}, EV_KEY=1, EV_SYN=0, SYN_DROPPED=3)


class Device:
    def __init__(self, path="/fake/keyboard", name="Test keyboard"):
        self.path = path
        self.name = name
        self.fd, self.write_fd = os.pipe()
        self.held = []
        self.grabbed = False
        self.closed = False
        self.error = None

    def fileno(self):
        return self.fd

    def capabilities(self):
        return {E.EV_KEY: [E.KEY_A, E.KEY_Z, E.KEY_ENTER]}

    def active_keys(self):
        return self.held

    def grab(self):
        self.grabbed = True

    def ungrab(self):
        self.grabbed = False

    def read(self):
        os.read(self.fd, 1)
        if self.error:
            raise self.error
        return []

    def close(self):
        if not self.closed:
            self.closed = True
            os.close(self.fd)
            os.close(self.write_fd)


class LinuxInputTests(unittest.TestCase):
    def make_keyboard(self, devices=None, callback=None):
        devices = devices if devices is not None else [Device()]
        module = SimpleNamespace(ecodes=E, list_devices=lambda: [d.path for d in devices], InputDevice=lambda path: next(d for d in devices if d.path == path), UInput=Mock(return_value=Mock()))
        actions = []
        keyboard = LinuxKeyboard(callback or actions.append, evdev_module=module)
        self.addCleanup(keyboard.close)
        for device in devices:
            self.addCleanup(device.close)
        return keyboard, module, actions, devices

    def press(self, keyboard, device, code, value=1):
        keyboard._handle_key(device, SimpleNamespace(code=code, value=value))

    def test_start_close_and_no_automatic_grab(self):
        keyboard, _, _, devices = self.make_keyboard()
        keyboard.start()
        self.assertTrue(keyboard.typing_available)
        self.assertFalse(devices[0].grabbed)
        keyboard.close()
        self.assertTrue(devices[0].closed)

    def test_arming_waits_for_release_and_escape_releases_immediately(self):
        keyboard, _, actions, devices = self.make_keyboard()
        keyboard.devices = devices
        keyboard.typing_available = True
        devices[0].held = [E.KEY_RIGHTSHIFT]
        keyboard.set_typing(True)
        keyboard._arm_if_released()
        self.assertFalse(devices[0].grabbed)
        devices[0].held = []
        keyboard._arm_if_released()
        self.assertTrue(devices[0].grabbed)
        self.press(keyboard, devices[0], E.KEY_A)
        self.press(keyboard, devices[0], E.KEY_A, value=2)
        self.assertEqual(actions.count("step"), 1)
        self.press(keyboard, devices[0], E.KEY_ESC)
        self.assertFalse(devices[0].grabbed)
        self.assertEqual(actions[-1], "pause")

    def test_partial_grab_failure_rolls_back(self):
        devices = [Device("/fake/one"), Device("/fake/two")]
        keyboard, _, actions, _ = self.make_keyboard(devices)
        keyboard.devices = devices
        keyboard.typing_available = True
        devices[1].grab = Mock(side_effect=OSError("already grabbed"))
        keyboard.set_typing(True)
        keyboard._arm_if_released()
        self.assertFalse(devices[0].grabbed)
        self.assertTrue(actions[-1].startswith("error:"))

    def test_emergency_quit_releases_before_callback(self):
        keyboard, _, actions, devices = self.make_keyboard()
        keyboard.devices = devices
        keyboard.typing_available = True
        keyboard.set_typing(True)
        keyboard._arm_if_released()
        keyboard.on_action = lambda action: actions.append((action, devices[0].grabbed))
        self.press(keyboard, devices[0], E.KEY_LEFTCTRL)
        self.press(keyboard, devices[0], E.KEY_LEFTSHIFT)
        self.press(keyboard, devices[0], E.KEY_Q)
        self.assertEqual(actions[-1], ("quit", False))

    def test_injection_releases_modifiers_and_supports_enter_tab(self):
        keyboard, module, _, devices = self.make_keyboard()
        keyboard.devices = devices
        keyboard.virtual = module.UInput()
        keyboard.typing_available = True
        keyboard.set_typing(True)
        keyboard._arm_if_released()
        keyboard.send_character("Я")
        calls = [c.args for c in keyboard.virtual.write.call_args_list]
        self.assertEqual(calls, [(E.EV_KEY, E.KEY_LEFTCTRL, 1), (E.EV_KEY, E.KEY_V, 1), (E.EV_KEY, E.KEY_V, 0), (E.EV_KEY, E.KEY_LEFTCTRL, 0)])
        keyboard.virtual.write.reset_mock()
        keyboard.send_character("\t")
        keyboard.send_character("\n")
        self.assertEqual([c.args[1] for c in keyboard.virtual.write.call_args_list], [E.KEY_TAB, E.KEY_TAB, E.KEY_ENTER, E.KEY_ENTER])

    def test_device_read_failure_releases_grab(self):
        failed = threading.Event()
        keyboard, _, _, devices = self.make_keyboard(callback=lambda action: failed.set() if action.startswith("error:") else None)
        keyboard.start()
        keyboard.set_typing(True)
        keyboard._arm_if_released()
        self.assertTrue(devices[0].grabbed)
        devices[0].error = OSError("device disconnected")
        os.write(devices[0].write_fd, b"x")
        self.assertTrue(failed.wait(timeout=1))
        self.assertFalse(devices[0].grabbed)
        with self.assertRaises(InputUnavailable):
            keyboard.set_typing(True)

    def test_read_only_access_retains_hotkeys(self):
        keyboard, module, actions, devices = self.make_keyboard()
        module.UInput.side_effect = PermissionError("uinput denied")
        keyboard.start()
        self.assertFalse(keyboard.typing_available)
        self.press(keyboard, devices[0], E.KEY_RIGHTSHIFT)
        self.press(keyboard, devices[0], E.KEY_P)
        self.assertEqual(actions[-1], "screenshot")
        with self.assertRaises(InputUnavailable):
            keyboard.set_typing(True)

    def test_failed_virtual_write_releases_physical_and_virtual_devices(self):
        keyboard, module, _, devices = self.make_keyboard()
        keyboard.devices = devices
        virtual = module.UInput()
        keyboard.virtual = virtual
        keyboard.typing_available = True
        keyboard.set_typing(True)
        keyboard._arm_if_released()
        cause = OSError("virtual device write failed")
        virtual.write.side_effect = [None, cause, None]
        with self.assertRaises(InputUnavailable) as raised:
            keyboard.send_character("A")
        self.assertIs(raised.exception.__cause__, cause)
        self.assertFalse(devices[0].grabbed)
        self.assertFalse(keyboard.typing_available)
        virtual.close.assert_called_once()
        self.assertEqual(virtual.write.call_args.args, (E.EV_KEY, E.KEY_LEFTCTRL, 0))

    def test_failed_ungrab_closes_descriptor(self):
        keyboard, _, _, devices = self.make_keyboard()
        keyboard.devices = devices
        keyboard.typing_available = True
        keyboard.set_typing(True)
        keyboard._arm_if_released()
        devices[0].ungrab = Mock(side_effect=OSError("ungrab failed"))
        with self.assertLogs("clonef.linux_input", level="WARNING"):
            keyboard.set_typing(False)
        self.assertTrue(devices[0].closed)
        self.assertFalse(keyboard.typing_available)
