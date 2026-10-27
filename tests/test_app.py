"""Оркестрация приложения: настоящий Tk + заглушка LM Studio + подставной winapi (Windows-вызовы заменены записью действий).
Проверяет порядок действий, потоки, отмену, буфер, ошибки. Настоящие вызовы Windows проверяет `--selftest` на Windows."""
import gc
import json
import os
import socket
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import tkinter as tk
except ImportError:  # pragma: no cover
    tk = None
try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

from screenfix import core  # noqa: E402
from screenfix.mock_lm import MockLM  # noqa: E402

HAVE_SCREEN = sys.platform == "win32" or bool(os.environ.get("DISPLAY"))


class FakeWinapi(types.ModuleType):
    def __init__(self):
        super().__init__("screenfix.winapi")
        self.selection = "Тут ашибка, щас исправим."
        self.clipboard = "старый буфер"
        self.log = []
        self.stopped = False
        self.q = None

    def start_hotkeys(self, combos, q):
        self.q, self.combos = q, combos
        return types.SimpleNamespace(stop=lambda: setattr(self, "stopped", True))

    def virtual_screen(self):
        return (0, 0, 800, 600)

    def cursor_pos(self):
        return (100, 100)

    def make_noactivate(self, wid):
        pass

    def get_clipboard_text(self):
        return self.clipboard

    def set_clipboard_text(self, t):
        self.clipboard = t
        self.log.append(("set", t))

    def copy_selection(self, timeout=1.2):
        self.log.append("copy")
        if self.selection is None:
            raise core.ScreenFixError("Не получилось скопировать выделенный текст.")
        return self.selection

    def paste_text(self, t):
        self.log.append(("paste", t))

    def message_box(self, *a, **k):
        pass


@unittest.skipUnless(tk is not None and Image is not None and HAVE_SCREEN, "нужны tkinter, Pillow и экран")
class AppFlow(unittest.TestCase):
    def setUp(self):
        self.fake = FakeWinapi()
        sys.modules["screenfix.winapi"] = self.fake
        import screenfix

        screenfix.winapi = self.fake  # type: ignore[attr-defined]
        from screenfix import app, ui

        self.app, self.ui = app, ui
        self.patch_ui = mock.patch.object(ui, "winapi", self.fake, create=True)
        self.patch_ui.start()

        self.mock = MockLM()
        self.url = self.mock.start()
        self.dir = tempfile.mkdtemp()
        self.write_config({})
        self.root = tk.Tk()
        self.ctl = app.Controller(self.root, self.dir)

    def tearDown(self):
        try:
            if not self.ctl.closing:
                self.ctl.quit()
        except tk.TclError:
            pass
        self.mock.stop()
        self.patch_ui.stop()
        sys.modules.pop("screenfix.winapi", None)
        import screenfix

        if hasattr(screenfix, "winapi"):
            delattr(screenfix, "winapi")
        self.ctl = None
        self.root = None
        gc.collect()  # объекты Tk нужно освобождать в главном потоке, иначе Tcl падает

    # ── помощники ──
    def write_config(self, extra):
        cfg = {"base_url": self.url, "model": "qwen3-8b"}
        cfg.update(extra)
        with open(os.path.join(self.dir, "config.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f)

    def pump(self, cond, timeout=6.0):
        end = time.time() + timeout
        while time.time() < end:
            self.root.update()
            if cond():
                return True
            time.sleep(0.01)
        self.root.update()
        return cond()

    def hotkey(self, name):
        self.ctl.events.put(name)  # так же, как это делает поток горячих клавиш

    def shown(self):
        return self.ctl.result.text.get("1.0", "end-1c")

    # ── сценарии ──
    def test_startup_registers_four_hotkeys_and_checks_server(self):
        self.assertEqual(set(self.fake.combos), set(core.HOTKEY_FIELDS))
        self.assertTrue(self.pump(lambda: "подключено" in self.ctl.panel.status.cget("text")))

    def test_fix_selection_end_to_end(self):
        self.hotkey("hotkey_fix")
        self.assertTrue(self.pump(lambda: self.ctl.result.mode == "result"), self.shown())
        self.assertEqual(self.fake.log[0], "copy")
        self.assertEqual(self.ctl.last_text, "Тут ошибка, сейчас исправим.")
        self.assertEqual(self.fake.clipboard, "Тут ошибка, сейчас исправим.")  # auto_copy
        self.assertIn("скопировано в буфер", self.ctl.result.title_var.get())
        self.assertTrue(self.pump(lambda: not self.ctl.busy))
        req = self.mock.requests[-1]
        self.assertEqual(req["messages"][1]["content"], "Тут ашибка, щас исправим.")
        self.assertIn("общепринятым нормам", req["messages"][0]["content"])  # rules.txt пуст
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "rules.txt")))

    def test_rules_file_changes_apply_without_restart(self):
        with open(os.path.join(self.dir, "rules.txt"), "w", encoding="utf-8") as f:
            f.write("# комментарий\n1. Букву «ё» не заменять на «е».\n")
        self.hotkey("hotkey_fix")
        self.assertTrue(self.pump(lambda: self.ctl.result.mode == "result"))
        self.assertIn("1. Букву «ё» не заменять на «е».", self.mock.requests[-1]["messages"][0]["content"])
        self.assertNotIn("комментарий", self.mock.requests[-1]["messages"][0]["content"])

    def test_without_auto_copy_previous_clipboard_is_restored(self):
        self.write_config({"auto_copy": False})
        self.hotkey("hotkey_fix")
        self.assertTrue(self.pump(lambda: self.ctl.result.mode == "result"))
        self.assertEqual(self.fake.clipboard, "старый буфер")
        self.assertEqual(self.ctl.last_text, "Тут ошибка, сейчас исправим.")

    def test_copy_failure_is_shown_and_app_stays_usable(self):
        self.fake.selection = None
        self.hotkey("hotkey_fix")
        self.assertTrue(self.pump(lambda: self.ctl.result.mode == "error"))
        self.assertIn("копировать", self.shown())
        self.assertTrue(self.pump(lambda: not self.ctl.busy))
        self.fake.selection = "щас"
        self.hotkey("hotkey_fix")
        self.assertTrue(self.pump(lambda: self.ctl.result.mode == "result"))
        self.assertEqual(self.ctl.last_text, "сейчас")

    def test_server_down_message(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.write_config({"base_url": f"http://127.0.0.1:{port}/v1"})
        self.hotkey("hotkey_fix")
        self.assertTrue(self.pump(lambda: self.ctl.result.mode == "error"))
        self.assertIn("LM Studio", self.shown())
        self.assertTrue(self.pump(lambda: not self.ctl.busy))

    def test_broken_config_is_reported_not_crash(self):
        with open(os.path.join(self.dir, "config.json"), "w", encoding="utf-8") as f:
            f.write("{не json")
        self.hotkey("hotkey_fix")
        self.assertTrue(self.pump(lambda: self.ctl.result.mode == "error"))
        self.assertIn("config.json", self.shown())
        self.assertFalse(self.ctl.busy)

    def test_second_hotkey_while_busy_is_ignored(self):
        self.mock.delay = 0.05
        self.hotkey("hotkey_fix")
        self.assertTrue(self.pump(lambda: self.ctl.busy and self.ctl.result.mode == "busy"))
        self.hotkey("hotkey_fix")
        self.assertTrue(self.pump(lambda: self.ctl.result.mode == "result", timeout=10))
        self.assertTrue(self.pump(lambda: not self.ctl.busy))
        self.assertEqual(len(self.mock.requests), 1)
        self.assertEqual(self.fake.log.count("copy"), 1)

    def test_streaming_updates_window_before_the_end(self):
        self.mock.delay = 0.05
        seen = []
        self.hotkey("hotkey_fix")
        self.pump(lambda: seen.append(self.shown()) or self.ctl.result.mode == "result", timeout=10)
        partials = [s for s in seen if s and s != "…" and s != "Тут ошибка, сейчас исправим."]
        self.assertTrue(partials, seen[-5:])

    def test_cancel_discards_late_result_and_frees_the_app(self):
        self.mock.delay = 0.08
        self.hotkey("hotkey_fix")
        self.assertTrue(self.pump(lambda: self.ctl.busy and self.ctl.result.mode == "busy" and self.shown() not in ("…", "")))
        self.ctl.result.btn_cancel.invoke()
        self.assertFalse(self.ctl.busy)
        self.assertFalse(self.ctl.result.visible)
        self.pump(lambda: False, timeout=1.2)  # дать отменённому потоку закончиться
        self.assertFalse(self.ctl.result.visible)  # запоздавший результат окно не показал
        self.assertIsNone(self.ctl.last_text)
        self.assertEqual(self.fake.clipboard, "старый буфер")  # и буфер не затёр
        self.mock.delay = 0
        self.fake.selection = "щас"
        self.hotkey("hotkey_fix")
        self.assertTrue(self.pump(lambda: self.ctl.result.mode == "result"))
        self.assertEqual(self.ctl.last_text, "сейчас")

    def test_paste_replaces_selection_with_last_result(self):
        self.hotkey("hotkey_paste")
        self.assertTrue(self.pump(lambda: self.ctl.result.mode == "error"))
        self.assertIn("нечего вставлять", self.shown().lower())
        self.hotkey("hotkey_fix")
        self.assertTrue(self.pump(lambda: self.ctl.result.mode == "result"))
        self.assertTrue(self.pump(lambda: not self.ctl.busy))
        self.hotkey("hotkey_paste")
        self.assertTrue(self.pump(lambda: ("paste", "Тут ошибка, сейчас исправим.") in self.fake.log))
        self.assertTrue(self.pump(lambda: not self.ctl.result.visible))

    def test_copy_button_puts_result_on_clipboard(self):
        self.write_config({"auto_copy": False})
        self.hotkey("hotkey_fix")
        self.assertTrue(self.pump(lambda: self.ctl.result.mode == "result"))
        self.assertEqual(self.fake.clipboard, "старый буфер")
        self.ctl.result.btn_copy.invoke()
        self.assertEqual(self.fake.clipboard, "Тут ошибка, сейчас исправим.")

    def test_screen_reading_flow(self):
        import PIL.ImageGrab

        picked = {}

        def recognize(img, lang, strict=False, timeout=40.0):
            picked["size"], picked["lang"], picked["strict"] = img.size, lang, strict
            return "Тут ашибка щас", "ru"

        with mock.patch.object(PIL.ImageGrab, "grab", lambda **k: Image.new("RGB", (200, 100), "white")), mock.patch.object(
            self.ui, "select_region", lambda root, image, origin: (10, 10, 110, 60)
        ), mock.patch.object(self.ctl.ocr, "recognize", recognize):
            self.hotkey("hotkey_ocr")
            self.assertTrue(self.pump(lambda: self.ctl.result.mode == "result"), self.shown())
        self.assertEqual(picked, {"size": (100, 50), "lang": "ru", "strict": True})
        self.assertNotIn("copy", self.fake.log)  # Ctrl+C в этом режиме не нажимается
        self.assertEqual(self.ctl.last_text, "Тут ошибка сейчас")
        self.assertIn("распознаванием", self.mock.requests[-1]["messages"][0]["content"])

    def test_screen_reading_cancelled_selection(self):
        import PIL.ImageGrab

        with mock.patch.object(PIL.ImageGrab, "grab", lambda **k: Image.new("RGB", (50, 50))), mock.patch.object(
            self.ui, "select_region", lambda root, image, origin: None
        ):
            self.hotkey("hotkey_ocr")
            self.assertTrue(self.pump(lambda: not self.ctl.busy and self.ctl.events.empty()))
        self.assertEqual(self.mock.requests, [])
        self.assertEqual(self.ctl.result.mode, "idle")

    def test_ocr_error_is_shown(self):
        import PIL.ImageGrab

        def boom(img, lang, strict=False, timeout=40.0):
            raise core.ScreenFixError("В Windows нет распознавания текста для языка «ru».")

        with mock.patch.object(PIL.ImageGrab, "grab", lambda **k: Image.new("RGB", (50, 50))), mock.patch.object(
            self.ui, "select_region", lambda root, image, origin: (0, 0, 20, 20)
        ), mock.patch.object(self.ctl.ocr, "recognize", boom):
            self.hotkey("hotkey_ocr")
            self.assertTrue(self.pump(lambda: self.ctl.result.mode == "error"))
        self.assertIn("распознавания", self.shown())

    def test_quit_hotkey_stops_hotkeys_and_destroys_window(self):
        self.hotkey("hotkey_quit")
        self.assertTrue(self.pump(lambda: self.ctl.closing))
        self.assertTrue(self.fake.stopped)
        with self.assertRaises(tk.TclError):
            self.root.winfo_exists()

    def test_conflicting_hotkeys_in_config_stop_startup_with_message(self):
        self.ctl.quit()
        self.write_config({"hotkey_paste": "ctrl+alt+k"})
        self.root = tk.Tk()
        try:
            with self.assertRaises(core.ScreenFixError) as cm:
                self.app.Controller(self.root, self.dir)
            self.assertIn("hotkey_paste", str(cm.exception))
        finally:
            self.root.destroy()
            self.ctl = types.SimpleNamespace(closing=True)

    def test_copy_probe_success_and_clipboard_restored(self):
        from screenfix import selftest

        self.fake.selection = "Тест копирования выделенного текста ёЁ"
        detail = selftest.copy_probe(self.root)
        self.assertIn("Ctrl+C", detail)
        self.assertEqual(self.fake.clipboard, "старый буфер")  # прежний буфер возвращён
        self.assertEqual([w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel) and w.title() == "ScreenFix: проверка копирования"], [])

    def test_copy_probe_failure_is_explained(self):
        from screenfix import selftest

        self.fake.selection = "совсем другой текст"
        with self.assertRaises(core.ScreenFixError) as cm:
            selftest.copy_probe(self.root)
        self.assertIn("не то", str(cm.exception))

    def test_selftest_button_runs_steps_and_shows_report(self):
        from screenfix import selftest

        self.fake.selection = "Тест копирования выделенного текста ёЁ"
        self.ctl.run_selftest()
        self.assertTrue(self.pump(lambda: os.path.isfile(os.path.join(self.dir, "selftest.txt")), timeout=30))
        with open(os.path.join(self.dir, "selftest.txt"), encoding="utf-8") as f:
            report = f.read()
        self.assertIn("[PASS] LM Studio: сервер и модель", report)
        self.assertIn("qwen3-8b", report)
        self.assertIn("[PASS] Копирование выделенного (Ctrl+C)", report)
        self.assertEqual(report.count("Копирование выделенного (Ctrl+C)"), 1)  # заглушка SKIP заменена результатом
        self.assertTrue(self.pump(lambda: "Самопроверка закончена" in self.ctl.panel.status.cget("text")))
        reports = [w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel) and w.title() == "ScreenFix: самопроверка"]
        self.assertEqual(len(reports), 1)
        self.assertEqual(selftest.COPY_STEP_NAME, "Копирование выделенного (Ctrl+C)")

    def test_log_does_not_contain_text_by_default(self):
        self.hotkey("hotkey_fix")
        self.assertTrue(self.pump(lambda: self.ctl.result.mode == "result"))
        with open(os.path.join(self.dir, "screenfix.log"), encoding="utf-8") as f:
            log = f.read()
        self.assertIn("ok режим=выделение", log)
        self.assertNotIn("ашибка", log)
        self.assertNotIn("ошибка,", log)


if __name__ == "__main__":
    unittest.main()
