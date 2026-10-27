"""Окна ScreenFix (tkinter): панель управления, окно результата, выбор области экрана, окно отчёта."""
from __future__ import annotations

import tkinter as tk
from typing import Callable, Dict, Optional, Tuple

from . import core
from .core import FixResult

try:  # winapi есть только в Windows; без него окна всё равно создаются (удобно для разработки)
    from . import winapi
except ImportError:  # pragma: no cover
    winapi = None  # type: ignore[assignment]

C = {
    "bg": "#ffffff",
    "fg": "#1f2328",
    "muted": "#6b7280",
    "border": "#aab2bb",
    "ins_bg": "#d4f4dd",
    "del_bg": "#fde8e8",
    "del_fg": "#b42318",
    "err": "#b42318",
    "ok": "#1a7f37",
    "btn": "#f3f4f6",
}
FONT = ("Segoe UI", 10)
FONT_TEXT = ("Segoe UI", 11)
FONT_SMALL = ("Segoe UI", 9)


def pretty_hotkey(spec: str) -> str:
    names = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win", "control": "Ctrl"}
    return "+".join(names.get(p.strip().lower(), p.strip().upper() if len(p.strip()) == 1 else p.strip().capitalize()) for p in spec.split("+"))


def _geom(w: int, h: int, x: int, y: int) -> str:
    # Tk понимает отрицательные координаты только в виде «+-1920»; запись «-1920» означает отсчёт от правого края
    return f"{w}x{h}+{x}+{y}"


def _screen_bounds(root: tk.Misc) -> Tuple[int, int, int, int]:
    if winapi is not None:
        x, y, w, h = winapi.virtual_screen()
        if w > 0 and h > 0:
            return x, y, w, h
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


def _button(parent: tk.Misc, text: str, command: Callable[[], None]) -> tk.Button:
    return tk.Button(
        parent, text=text, command=command, font=FONT_SMALL, bg=C["btn"], fg=C["fg"], relief="groove", bd=1, padx=10, pady=3, cursor="hand2"
    )


# ── окно результата ──────────────────────────────────────────────────────────────────────────────

class ResultWindow:
    """Маленькое окно рядом с курсором. Оно НЕ забирает фокус у программы, из которой взят текст:
    выделение остаётся на месте, и «Вставить на место» заменяет именно его."""

    WIDTH = 580  # в пикселях при 96 dpi; при другом масштабе экрана пересчитывается
    MAX_LINES = 18

    def __init__(self, root: tk.Tk, on_copy: Callable[[], None], on_paste: Callable[[], None], on_cancel: Callable[[], None]) -> None:
        self.root = root
        self.on_copy, self.on_paste, self.on_cancel = on_copy, on_paste, on_cancel
        self.fix: Optional[FixResult] = None
        self.show_diff = True
        self.mode = "idle"
        self.visible = False
        self._xy: Optional[Tuple[int, int]] = None
        try:
            self.k = max(1.0, float(root.winfo_fpixels("1i")) / 96.0)  # масштаб экрана: 1.0 = 100 %, 1.5 = 150 %
        except tk.TclError:
            self.k = 1.0
        self.width = int(self.WIDTH * self.k)

        self.win = tk.Toplevel(root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=C["border"])
        inner = tk.Frame(self.win, bg=C["bg"])
        inner.pack(padx=1, pady=1, fill="both", expand=True)

        self.title_var = tk.StringVar()
        self.title_lbl = tk.Label(inner, textvariable=self.title_var, anchor="w", justify="left", bg=C["bg"], fg=C["muted"], font=FONT_SMALL, wraplength=self.width - int(20 * self.k))
        self.title_lbl.pack(fill="x", padx=10, pady=(8, 0))
        self.text = tk.Text(
            inner, wrap="word", width=1, height=3, relief="flat", bd=0, highlightthickness=0, bg=C["bg"], fg=C["fg"], font=FONT_TEXT,
            padx=10, pady=6, cursor="arrow", takefocus=0,
        )
        self.text.pack(fill="both", expand=True)
        self.text.tag_configure("ins", background=C["ins_bg"])
        self.text.tag_configure("del", background=C["del_bg"], foreground=C["del_fg"], overstrike=True)
        self.text.tag_configure("muted", foreground=C["muted"])
        self.text.tag_configure("err", foreground=C["err"])
        self.text.configure(state="disabled")

        self.bar = tk.Frame(inner, bg=C["bg"])
        self.bar.pack(fill="x", padx=8, pady=(0, 8))
        self.btn_copy = _button(self.bar, "Копировать", lambda: self.on_copy())
        self.btn_paste = _button(self.bar, "Вставить на место", lambda: self.on_paste())
        self.btn_diff = _button(self.bar, "Правки: показать", self._toggle_diff)
        self.btn_cancel = _button(self.bar, "Отмена", lambda: self.on_cancel())
        self.btn_close = _button(self.bar, "Закрыть", self.hide)

        self.win.update_idletasks()
        if winapi is not None:
            try:
                winapi.make_noactivate(self.win.winfo_id())
            except Exception:
                pass  # без этого окно просто может забрать фокус: работа программы не ломается

    # ── состояния ──
    def _buttons(self, *buttons: tk.Button) -> None:
        for b in (self.btn_copy, self.btn_paste, self.btn_diff, self.btn_cancel, self.btn_close):
            b.pack_forget()
        for b in buttons:
            b.pack(side="left", padx=3)

    def show_status(self, message: str) -> None:
        self.mode = "busy"
        self.title_var.set(message)
        self.title_lbl.configure(fg=C["muted"])
        self._set_plain("…", "muted")
        self._buttons(self.btn_cancel)
        self._show()

    def set_busy_title(self, message: str) -> None:
        if self.mode == "busy":
            self.title_var.set(message)

    def show_stream(self, partial: str, message: str = "Исправляю…") -> None:
        if self.mode != "busy":
            return  # запоздавший кусок после отмены или ошибки
        self.title_var.set(message)
        self._set_plain(partial or "…", None if partial else "muted")

    def show_result(self, fix: FixResult, note: str) -> None:
        self.mode = "result"
        self.fix = fix
        if fix.changed:
            head = "Исправлено"
            self.title_lbl.configure(fg=C["ok"])
        else:
            head = "Ошибок не найдено"
            self.title_lbl.configure(fg=C["muted"])
        speed = f"{fix.total_s:.1f} с" + (f" (первый ответ {fix.first_token_s:.1f} с)" if fix.first_token_s is not None else "")
        self.title_var.set(f"{head} · {speed} · {fix.model}" + (f" · {note}" if note else ""))
        self._render_result()
        self._buttons(self.btn_copy, self.btn_paste, *( [self.btn_diff] if fix.changed else [] ), self.btn_close)
        self._show()

    def show_error(self, message: str) -> None:
        self.mode = "error"
        self.fix = None
        self.title_var.set("Не получилось")
        self.title_lbl.configure(fg=C["err"])
        self._set_plain(message, "err")
        self._buttons(self.btn_close)
        self._show()

    def hide(self) -> None:
        self.visible = False
        self.win.withdraw()

    # ── содержимое ──
    def _toggle_diff(self) -> None:
        self.show_diff = not self.show_diff
        self._render_result()

    def _render_result(self) -> None:
        fix = self.fix
        if fix is None:
            return
        self.btn_diff.configure(text="Правки: скрыть" if self.show_diff else "Правки: показать")
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        if self.show_diff and fix.changed:
            for op, piece in core.diff_tokens(fix.original, fix.corrected):
                self.text.insert("end", piece, () if op == "eq" else (op,))
        else:
            self.text.insert("end", fix.corrected)
        self.text.configure(state="disabled")
        self._fit()

    def _set_plain(self, content: str, tag: Optional[str]) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("end", content, (tag,) if tag else ())
        self.text.configure(state="disabled")
        self._fit()

    def _fit(self) -> None:
        """Высота по числу строк, которое насчитал сам Tk с учётом переносов, шрифта и масштаба экрана."""
        self.win.update_idletasks()
        if self.text.winfo_width() < self.width // 2:  # окно ещё не получило ширину (не показывалось): задаём её явно
            self.win.geometry(_geom(self.width, max(self.win.winfo_reqheight(), 60), *(self._xy or (0, 0))))
            self.win.update_idletasks()
        try:
            # Tk считает «строки между индексами»; до "end" (после последнего перевода строки) это ровно число строк
            counted = self.text.count("1.0", "end", "displaylines")
            lines = int(counted[0]) if counted else 1
        except (tk.TclError, TypeError, ValueError, IndexError):
            lines = 4
        height = max(2, min(self.MAX_LINES, lines))
        self.text.configure(height=height)
        # Страховка для уже показанного окна: если последний символ не виден, добавляем строки (до предела)
        if self.win.winfo_ismapped():
            self.win.update_idletasks()
            while height < self.MAX_LINES and self.text.bbox("end-1c") is None:
                height += 1
                self.text.configure(height=height)
                self.win.update_idletasks()
        self._place()

    # ── положение ──
    def _show(self) -> None:
        if not self.visible:
            self.visible = True
            self._xy = None
            self._place()
            self.win.deiconify()
            self.win.lift()
        else:
            self._place()

    def _place(self) -> None:
        self.win.update_idletasks()
        w = max(self.width, self.win.winfo_reqwidth())
        h = self.win.winfo_reqheight()
        sx, sy, sw, sh = _screen_bounds(self.root)
        if self._xy is None:
            cx, cy = winapi.cursor_pos() if winapi is not None else (sx + sw // 2, sy + sh // 2)
            x, y = cx + 16, cy + 16
            if y + h > sy + sh - 8:  # не помещается снизу — ставим над курсором
                y = cy - h - 16
            self._xy = (x, y)
        x, y = self._xy
        x = max(sx + 8, min(x, sx + sw - w - 8))
        y = max(sy + 8, min(y, sy + sh - h - 8))
        self.win.geometry(_geom(w, h, x, y))


# ── выбор области экрана ─────────────────────────────────────────────────────────────────────────

def select_region(root: tk.Tk, image, origin: Tuple[int, int]) -> Optional[Tuple[int, int, int, int]]:
    """Показывает снимок экрана на весь виртуальный экран; пользователь протягивает прямоугольник мышью.
    Возвращает (левый, верхний, правый, нижний) в пикселях снимка либо None, если нажали Esc или правую кнопку."""
    import base64
    import io

    from PIL import ImageEnhance

    w, h = image.size
    top = tk.Toplevel(root)
    top.overrideredirect(True)
    top.attributes("-topmost", True)
    top.geometry(_geom(w, h, origin[0], origin[1]))
    canvas = tk.Canvas(top, width=w, height=h, highlightthickness=0, bd=0, cursor="crosshair", bg="black")
    canvas.pack()
    dimmed = ImageEnhance.Brightness(image.convert("RGB")).enhance(0.7)
    buf = io.BytesIO()
    dimmed.save(buf, "PNG", compress_level=1)  # Tk показывает PNG сам, без ImageTk
    photo = tk.PhotoImage(master=top, data=base64.b64encode(buf.getvalue()).decode("ascii"))
    canvas.create_image(0, 0, image=photo, anchor="nw")
    hint_x = min(w // 2, 700)
    canvas.create_text(hint_x, 24, text="Протяните рамку вокруг текста. Esc или правая кнопка мыши — отмена.", fill="#ffffff", font=("Segoe UI", 13, "bold"))

    state: Dict[str, object] = {"start": None, "rect": None, "result": None}

    def on_press(e: tk.Event) -> None:
        state["start"] = (e.x, e.y)
        if state["rect"] is not None:
            canvas.delete(state["rect"])
        state["rect"] = canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="#ff3b30", width=2)

    def on_move(e: tk.Event) -> None:
        if state["start"] is not None and state["rect"] is not None:
            sx, sy = state["start"]  # type: ignore[misc]
            canvas.coords(state["rect"], sx, sy, e.x, e.y)

    def on_release(e: tk.Event) -> None:
        if state["start"] is None:
            return
        sx, sy = state["start"]  # type: ignore[misc]
        state["start"] = None
        l, r = sorted((sx, e.x))
        t, b = sorted((sy, e.y))
        if r - l < 8 or b - t < 8:  # случайный щелчок
            if state["rect"] is not None:
                canvas.delete(state["rect"])
                state["rect"] = None
            return
        # окно могло получить размер, отличный от снимка (масштаб экрана): пересчитываем координаты
        kx = w / float(max(1, canvas.winfo_width()))
        ky = h / float(max(1, canvas.winfo_height()))
        state["result"] = (int(l * kx), int(t * ky), int(r * kx), int(b * ky))
        top.destroy()

    def cancel(_e: Optional[tk.Event] = None) -> None:
        state["result"] = None
        top.destroy()

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_move)
    canvas.bind("<ButtonRelease-1>", on_release)
    canvas.bind("<Button-3>", cancel)
    top.bind("<Escape>", cancel)
    canvas.bind("<Escape>", cancel)
    top.update_idletasks()
    try:
        top.focus_force()
        canvas.focus_set()
        top.wait_visibility()
        top.grab_set()
    except tk.TclError:
        pass  # без захвата всё работает, только Esc может потребовать щелчка по окну
    root.wait_window(top)
    del photo
    return state["result"]  # type: ignore[return-value]


# ── панель управления и отчёт ────────────────────────────────────────────────────────────────────

class ControlPanel:
    def __init__(self, root: tk.Tk, hotkeys: Dict[str, str], actions: Dict[str, Callable[[], None]]) -> None:
        self.root = root
        root.title(f"ScreenFix {core.VERSION}")
        root.configure(bg=C["bg"])
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", root.iconify)  # крестик сворачивает окно, программа продолжает работать

        tk.Label(root, text="ScreenFix", font=("Segoe UI", 14, "bold"), bg=C["bg"], fg=C["fg"]).grid(row=0, column=0, columnspan=2, sticky="w", padx=14, pady=(12, 0))
        tk.Label(
            root, text="Работает в фоне. Крестик сворачивает окно; полностью выйти — кнопка «Выход».", font=FONT_SMALL, bg=C["bg"], fg=C["muted"],
            wraplength=430, justify="left",
        ).grid(row=1, column=0, columnspan=2, sticky="w", padx=14, pady=(0, 8))

        rows = [
            ("Исправить выделенный текст", "hotkey_fix"),
            ("Прочитать область экрана и исправить", "hotkey_ocr"),
            ("Вставить результат вместо выделенного", "hotkey_paste"),
            ("Выйти из программы", "hotkey_quit"),
        ]
        for i, (label, key) in enumerate(rows, start=2):
            tk.Label(root, text=label, font=FONT, bg=C["bg"], fg=C["fg"], anchor="w").grid(row=i, column=0, sticky="w", padx=(14, 8), pady=1)
            tk.Label(root, text=pretty_hotkey(hotkeys[key]), font=("Consolas", 10, "bold"), bg=C["bg"], fg=C["fg"], anchor="e").grid(row=i, column=1, sticky="e", padx=(0, 14), pady=1)

        self.status = tk.Label(root, text="LM Studio: не проверялся.", font=FONT_SMALL, bg=C["bg"], fg=C["muted"], wraplength=430, justify="left", anchor="w")
        self.status.grid(row=6, column=0, columnspan=2, sticky="we", padx=14, pady=(10, 4))

        bar = tk.Frame(root, bg=C["bg"])
        bar.grid(row=7, column=0, columnspan=2, sticky="w", padx=10, pady=(0, 12))
        for text, key in [
            ("Проверить LM Studio", "check"),
            ("Правила", "rules"),
            ("Настройки", "config"),
            ("Самопроверка", "selftest"),
            ("Выход", "quit"),
        ]:
            _button(bar, text, actions[key]).pack(side="left", padx=3)

    def set_status(self, message: str, ok: Optional[bool] = None) -> None:
        self.status.configure(text=message, fg=C["muted"] if ok is None else (C["ok"] if ok else C["err"]))


def show_report(root: tk.Tk, title: str, text: str) -> None:
    """Окно с текстом, который можно выделить и скопировать (отчёт самопроверки)."""
    win = tk.Toplevel(root)
    win.title(title)
    win.configure(bg=C["bg"])
    box = tk.Text(win, wrap="word", width=96, height=26, font=("Consolas", 10), bg=C["bg"], fg=C["fg"], padx=8, pady=6)
    box.pack(fill="both", expand=True, padx=8, pady=(8, 4))
    box.insert("end", text)
    bar = tk.Frame(win, bg=C["bg"])
    bar.pack(fill="x", padx=8, pady=(0, 8))

    def copy_all() -> None:
        win.clipboard_clear()
        win.clipboard_append(text)

    _button(bar, "Копировать всё", copy_all).pack(side="left", padx=3)
    _button(bar, "Закрыть", win.destroy).pack(side="left", padx=3)
