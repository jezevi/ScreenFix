"""Проверка ЛОГИКИ winapi.py на любой ОС: сам WinAPI подменён заглушкой, поэтому настоящие вызовы Windows здесь
не проверяются (это делает `ScreenFix.exe --selftest` на Windows). Тест ловит ошибки в порядке действий,
ожиданиях и сообщениях, а также проверяет размер структуры INPUT, от которой зависит SendInput."""
import ctypes
import importlib
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from screenfix import core  # noqa: E402


def load_winapi():
    sys.modules.pop("screenfix.winapi", None)
    with mock.patch.object(sys, "platform", "win32"), mock.patch.object(ctypes, "WinDLL", mock.MagicMock(), create=True):
        return importlib.import_module("screenfix.winapi")


class WinapiLogic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.w = load_winapi()

    def setUp(self):
        self.sent = []
        self.seq = [100]
        self.clip = [None]
        self.held = set()
        w = self.w
        self.patches = [
            mock.patch.object(w, "send_keys", lambda ev: self.sent.append(list(ev))),
            mock.patch.object(w, "clipboard_seq", lambda: self.seq[0]),
            mock.patch.object(w, "get_clipboard_text", lambda: self.clip[0]),
            mock.patch.object(w, "_is_down", lambda vk: vk in self.held),
            mock.patch.object(w.time, "sleep", lambda s: None),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_input_struct_size_is_40_on_64bit(self):
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            self.assertEqual(ctypes.sizeof(self.w._INPUT), 40)
            self.assertEqual(ctypes.sizeof(self.w._KEYBDINPUT), 24)
        else:
            self.assertEqual(ctypes.sizeof(self.w._INPUT), 28)

    def test_copy_sends_ctrl_c_and_returns_text(self):
        w = self.w

        def send(ev):
            self.sent.append(list(ev))
            self.seq[0] += 1  # программа положила текст в буфер
            self.clip[0] = "Выделенный текст"

        with mock.patch.object(w, "send_keys", send):
            self.assertEqual(w.copy_selection(), "Выделенный текст")
        self.assertEqual(self.sent[0], [(w.VK_CONTROL, True), (w.VK_C, True), (w.VK_C, False), (w.VK_CONTROL, False)])

    def test_copy_waits_for_second_clipboard_update(self):
        w = self.w
        calls = {"n": 0}

        def get_text():
            calls["n"] += 1
            return "" if calls["n"] < 3 else "Готово"  # сначала буфер пуст (программа чистит его), затем текст

        def send(ev):
            self.seq[0] += 1

        with mock.patch.object(w, "send_keys", send), mock.patch.object(w, "get_clipboard_text", get_text):
            self.assertEqual(w.copy_selection(), "Готово")

    def test_copy_nothing_selected_is_explained(self):
        w = self.w
        ticks = iter(range(0, 10_000))
        with mock.patch.object(w.time, "monotonic", lambda: next(ticks) * 0.05):
            with self.assertRaises(core.ScreenFixError) as cm:
                w.copy_selection(timeout=1.0)
        msg = str(cm.exception)
        self.assertIn("выделен", msg)
        self.assertIn("администратора", msg)

    def test_copy_non_text_is_explained(self):
        w = self.w

        def send(ev):
            self.seq[0] += 1
            self.clip[0] = None

        with mock.patch.object(w, "send_keys", send):
            with self.assertRaises(core.ScreenFixError) as cm:
                w.copy_selection()
        self.assertIn("не текст", str(cm.exception))

    def test_held_modifiers_are_released_before_ctrl_c(self):
        w = self.w
        self.held = {w.VK_CONTROL, w.VK_MENU}
        ticks = iter(range(0, 10_000))
        with mock.patch.object(w.time, "monotonic", lambda: next(ticks) * 0.1):
            w.wait_modifiers_released(timeout=1.0)
        self.assertEqual(self.sent[-1], [(w.VK_CONTROL, False), (w.VK_MENU, False)])

    def test_released_modifiers_need_no_events(self):
        self.w.wait_modifiers_released()
        self.assertEqual(self.sent, [])

    def test_paste_sets_clipboard_then_sends_ctrl_v(self):
        w = self.w
        order = []
        with mock.patch.object(w, "set_clipboard_text", lambda t: order.append(("set", t))), mock.patch.object(
            w, "send_keys", lambda ev: order.append(("keys", list(ev)))
        ):
            w.paste_text("Результат")
        self.assertEqual(order[0], ("set", "Результат"))
        self.assertEqual(order[1], ("keys", [(w.VK_CONTROL, True), (w.VK_V, True), (w.VK_V, False), (w.VK_CONTROL, False)]))


if __name__ == "__main__":
    unittest.main()
