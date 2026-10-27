"""Проверка окон на настоящем Tk. Пропускается, если tkinter или экран недоступны (на Linux запускается через xvfb-run)."""
import gc
import os
import sys
import unittest

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

HAVE_SCREEN = sys.platform == "win32" or bool(os.environ.get("DISPLAY"))


@unittest.skipUnless(tk is not None and Image is not None and HAVE_SCREEN, "нужны tkinter, Pillow и экран")
class UiTests(unittest.TestCase):
    def setUp(self):
        from screenfix import ui

        self.ui = ui
        self.root = tk.Tk()
        self.events = []

    def tearDown(self):
        self.root.destroy()
        self.root = None
        gc.collect()  # объекты Tk нужно освобождать в главном потоке, иначе Tcl падает

    def make_result_window(self):
        return self.ui.ResultWindow(self.root, lambda: self.events.append("copy"), lambda: self.events.append("paste"), lambda: self.events.append("cancel"))

    def text_of(self, rw):
        return rw.text.get("1.0", "end-1c")

    def test_result_window_full_cycle(self):
        rw = self.make_result_window()
        rw.show_status("Подключаюсь…")
        self.assertEqual(rw.mode, "busy")
        self.assertTrue(rw.visible)
        rw.show_stream("Тут ошиб")
        self.assertEqual(self.text_of(rw), "Тут ошиб")

        fix = core.FixResult(original="Тут ашибка", corrected="Тут ошибка", model="qwen3-8b", first_token_s=0.4, total_s=1.2)
        rw.show_result(fix, "скопировано")
        self.assertEqual(rw.mode, "result")
        self.assertIn("Исправлено", rw.title_var.get())
        self.assertIn("qwen3-8b", rw.title_var.get())
        # в режиме правок виден и старый, и новый вариант; подсветка есть
        shown = self.text_of(rw)
        self.assertIn("ашибка", shown)
        self.assertIn("ошибка", shown)
        self.assertTrue(rw.text.tag_ranges("ins"))
        self.assertTrue(rw.text.tag_ranges("del"))
        rw._toggle_diff()
        self.assertEqual(self.text_of(rw), "Тут ошибка")
        rw._toggle_diff()

        # запоздавший кусок стрима после результата не должен затирать результат
        rw.show_stream("мусор")
        self.assertNotEqual(self.text_of(rw), "мусор")

        same = core.FixResult(original="Всё верно.", corrected="Всё верно.", model="m", first_token_s=None, total_s=0.5)
        rw.show_result(same, "")
        self.assertIn("Ошибок не найдено", rw.title_var.get())
        self.assertEqual(self.text_of(rw), "Всё верно.")

        rw.show_error("LM Studio не отвечает.")
        self.assertEqual(rw.mode, "error")
        self.assertEqual(self.text_of(rw), "LM Studio не отвечает.")
        rw.hide()
        self.assertFalse(rw.visible)

    def test_buttons_call_callbacks(self):
        rw = self.make_result_window()
        rw.btn_copy.invoke()
        rw.btn_paste.invoke()
        rw.btn_cancel.invoke()
        self.assertEqual(self.events, ["copy", "paste", "cancel"])

    def test_long_text_is_bounded_and_window_stays_on_screen(self):
        rw = self.make_result_window()
        fix = core.FixResult(original="слово " * 800, corrected="слово " * 800, model="m", first_token_s=0.1, total_s=0.2)
        rw.show_result(fix, "")
        self.root.update()
        self.assertLessEqual(int(rw.text.cget("height")), 18)
        sx, sy, sw, sh = self.ui._screen_bounds(self.root)
        self.assertGreaterEqual(rw.win.winfo_x(), sx)
        self.assertGreaterEqual(rw.win.winfo_y(), sy)

    def last_char_visible(self, rw):
        self.root.update()
        return rw.text.bbox("end-1c") is not None

    def test_whole_text_is_visible_at_any_screen_scale(self):
        paragraphs = [
            "Это довольно длинное предложение для проверки переносов строк в окне результата. " * n + "\nВторой абзац, короткий."
            for n in (1, 3, 6)
        ]
        for scaling in (1.333, 2.0, 3.0):  # примерно 96, 144, 192, 288 dpi
            for text in paragraphs:
                self.root.tk.call("tk", "scaling", scaling)
                rw = self.make_result_window()
                fix = core.FixResult(original=text, corrected=text, model="m", first_token_s=0.1, total_s=0.2)
                rw.show_result(fix, "")
                visible = self.last_char_visible(rw)
                height = int(rw.text.cget("height"))
                self.assertTrue(visible or height == rw.MAX_LINES, f"scaling={scaling}, знаков={len(text)}: последняя строка обрезана, height={height}")
                rw.win.destroy()

    def test_streaming_keeps_the_end_of_text_visible(self):
        rw = self.make_result_window()
        rw.show_status("…")
        for n in range(1, 60, 4):
            rw.show_stream("слово, " * n)
            self.assertTrue(self.last_char_visible(rw) or int(rw.text.cget("height")) == rw.MAX_LINES, n)

    def test_streaming_updates_do_not_move_the_window(self):
        rw = self.make_result_window()
        rw.show_status("…")
        self.root.update()
        pos = (rw.win.winfo_x(), rw.win.winfo_y())
        for n in range(1, 30):
            rw.show_stream("слово " * n)
            self.root.update()
        self.assertEqual((rw.win.winfo_x(), rw.win.winfo_y()), pos)

    def test_control_panel(self):
        cfg = core.load_config(os.path.join(os.path.dirname(__file__), "_none"), create=False)
        hotkeys = {k: str(cfg[k]) for k in core.HOTKEY_FIELDS}
        called = []
        actions = {k: (lambda k=k: called.append(k)) for k in ("check", "rules", "config", "selftest", "quit")}
        panel = self.ui.ControlPanel(self.root, hotkeys, actions)
        panel.set_status("LM Studio: подключено (qwen3-8b).", ok=True)
        self.root.update()
        self.assertIn("подключено", panel.status.cget("text"))
        self.assertEqual(self.ui.pretty_hotkey("ctrl+alt+k"), "Ctrl+Alt+K")
        self.assertEqual(self.ui.pretty_hotkey("win+f9"), "Win+F9")

    def test_select_region_drag_and_cancel(self):
        img = Image.new("RGB", (400, 300), "white")

        def drive(events):
            def go():
                tops = [w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel)]
                top = tops[-1]
                canvas = [c for c in top.winfo_children() if isinstance(c, tk.Canvas)][0]
                events(top, canvas)

            self.root.after(150, go)

        def drag(top, canvas):
            canvas.event_generate("<ButtonPress-1>", x=20, y=30)
            canvas.event_generate("<B1-Motion>", x=200, y=120)
            canvas.event_generate("<ButtonRelease-1>", x=200, y=120)

        drive(drag)
        box = self.ui.select_region(self.root, img, (0, 0))
        self.assertEqual(box, (20, 30, 200, 120))

        # обратное направление: справа налево и снизу вверх
        def drag_back(top, canvas):
            canvas.event_generate("<ButtonPress-1>", x=300, y=250)
            canvas.event_generate("<B1-Motion>", x=100, y=100)
            canvas.event_generate("<ButtonRelease-1>", x=100, y=100)

        drive(drag_back)
        self.assertEqual(self.ui.select_region(self.root, img, (0, 0)), (100, 100, 300, 250))

        # случайный щелчок игнорируется, затем Esc отменяет
        def click_then_esc(top, canvas):
            canvas.event_generate("<ButtonPress-1>", x=50, y=50)
            canvas.event_generate("<ButtonRelease-1>", x=52, y=52)
            top.event_generate("<Escape>")

        drive(click_then_esc)
        self.assertIsNone(self.ui.select_region(self.root, img, (0, 0)))

    def test_report_window(self):
        self.ui.show_report(self.root, "Отчёт", "строка 1\nстрока 2")
        self.root.update()
        tops = [w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel)]
        self.assertTrue(tops)


if __name__ == "__main__":
    unittest.main()
