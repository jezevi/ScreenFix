"""Самопроверка ScreenFix: что работает именно на этом компьютере.

Запуск: «ScreenFix.exe --selftest» (из командной строки) или кнопка «Самопроверка» в окне программы.
Результат: список «PASS / WARN / FAIL / SKIP» и файл selftest.txt рядом с программой.
"""
from __future__ import annotations

import os
import queue
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Callable, List, Optional

from . import core

IS_WIN = sys.platform == "win32"
SAMPLE_TEXT = "Я хател узнать пре сроки , потомучто вчира не успел."


class Skip(Exception):
    pass


class Warn(Exception):
    pass


@dataclass
class Result:
    name: str
    status: str  # PASS | WARN | FAIL | SKIP
    detail: str = ""


def _run(report: List[Result], name: str, fn: Callable[[], Optional[str]]) -> None:
    try:
        detail = fn() or ""
        report.append(Result(name, "PASS", detail))
    except Skip as e:
        report.append(Result(name, "SKIP", str(e)))
    except Warn as e:
        report.append(Result(name, "WARN", str(e)))
    except core.ScreenFixError as e:
        report.append(Result(name, "FAIL", str(e)))
    except Exception as e:  # самопроверка не должна падать сама: показываем, что именно сломалось
        tb = traceback.format_exc().strip().splitlines()
        report.append(Result(name, "FAIL", f"{e.__class__.__name__}: {e} ({tb[-3].strip() if len(tb) >= 3 else ''})"))


COPY_STEP_NAME = "Копирование выделенного (Ctrl+C)"


def copy_probe(root) -> str:
    """Проверяет настоящую эмуляцию Ctrl+C: окно с полем ввода, выделенный текст, копирование в буфер.
    Вызывать из главного потока Tk. При неудаче бросает ScreenFixError, при успехе возвращает описание."""
    import tkinter as tk

    from . import winapi

    sample = "Тест копирования выделенного текста ёЁ"
    top = tk.Toplevel(root)
    top.title("ScreenFix: проверка копирования")
    top.geometry("420x70+120+120")
    previous = None
    out: dict = {}
    try:
        previous = winapi.get_clipboard_text()
        entry = tk.Entry(top, width=50)
        entry.pack(padx=10, pady=10)
        entry.insert(0, sample)
        top.update()
        top.lift()
        top.focus_force()
        entry.focus_force()
        entry.select_range(0, "end")
        top.update()
        time.sleep(0.15)
        top.update()

        def worker() -> None:
            try:
                out["text"] = winapi.copy_selection()
            except Exception as e:  # noqa: BLE001
                out["err"] = e

        th = threading.Thread(target=worker, daemon=True)
        th.start()
        deadline = time.time() + 6
        while th.is_alive() and time.time() < deadline:
            top.update()
            time.sleep(0.01)
    finally:
        top.destroy()
        if previous is not None:
            try:
                winapi.set_clipboard_text(previous)
            except core.ScreenFixError:
                pass
    if "err" in out:
        raise core.ScreenFixError(f"Ctrl+C не сработал: {out['err']}")
    if out.get("text") != sample:
        raise core.ScreenFixError(f"Скопировано не то: {out.get('text')!r}")
    return "эмуляция Ctrl+C копирует выделенный текст из окна"


def replace_result(report: List[Result], new: Result) -> None:
    for i, r in enumerate(report):
        if r.name == new.name:
            report[i] = new
            return
    report.append(new)


def run_all(directory: str, gui: bool = True, hotkeys_free: bool = True) -> List[Result]:
    """gui=False — пропустить проверки с отдельными окнами Tk (их нельзя запускать внутри работающей программы).
    hotkeys_free=False — не регистрировать горячие клавиши (их уже держит запущенная программа)."""
    report: List[Result] = []
    cfg_holder: dict = {}

    def info() -> str:
        import platform

        return f"ScreenFix {core.VERSION}; Python {platform.python_version()}; {platform.platform()}; папка {directory}"

    def config_and_rules() -> str:
        cfg = core.load_config(directory, create=False)
        cfg_holder["cfg"] = cfg
        combos = core.parse_all_hotkeys(cfg)
        rules = core.load_rules(directory, create=False)
        keys = ", ".join(f"{k.replace('hotkey_', '')}={cfg[k]}" for k in core.HOTKEY_FIELDS)
        if not rules:
            raise Warn(
                f"Клавиши: {keys}. Файл rules.txt пуст или не найден: текст будет исправляться по общим нормам русского языка, "
                "а не по правилам площадки. Вставьте правила в rules.txt."
            )
        return f"Клавиши: {keys}. Правила: {len(rules.splitlines())} строк, {len(rules)} знаков ({len(combos)} клавиши разобраны)."

    def core_with_mock() -> str:
        from .mock_lm import MockLM

        mock = MockLM()
        url = mock.start()
        try:
            cfg = core.load_config(tempfile.mkdtemp(), create=False)
            cfg["base_url"] = url
            mock.think = "inline"
            res = core.run_fix(cfg, "1. Правило", "Тут ашибка, щас исправим.")
            if res.corrected != "Тут ошибка, сейчас исправим.":
                raise core.ScreenFixError(f"Неверный результат: {res.corrected!r}")
        finally:
            mock.stop()
        return "запрос, потоковый ответ и вырезание рассуждений работают (на заглушке сервера)"

    def lm_models() -> str:
        cfg = cfg_holder.get("cfg") or core.load_config(directory, create=False)
        try:
            ids = core.list_models(cfg)
            model = core.pick_model(cfg)
        except core.ScreenFixError as e:
            raise Warn(str(e))
        cfg_holder["model_ok"] = True
        return f"модели: {', '.join(ids) or '—'}; будет использована: {model}"

    def lm_fix() -> str:
        if not cfg_holder.get("model_ok"):
            raise Skip("LM Studio недоступен")
        cfg = cfg_holder.get("cfg") or core.load_config(directory, create=False)
        rules = core.load_rules(directory, create=False)
        res = core.run_fix(cfg, rules, SAMPLE_TEXT)
        speed = f"{res.total_s:.1f} с" + (f", первый ответ через {res.first_token_s:.1f} с" if res.first_token_s is not None else "")
        if not res.changed:
            raise Warn(f"Модель не исправила ни одной из явных ошибок. Ответ: {res.corrected!r}. {speed}")
        return f"{speed}. Было: {SAMPLE_TEXT!r} Стало: {res.corrected!r}"

    def clipboard() -> str:
        if not IS_WIN:
            raise Skip("только Windows")
        from . import winapi

        previous = winapi.get_clipboard_text()
        sample = "Проверка буфера: ёЁ — «кавычки»\nвторая строка"
        try:
            winapi.set_clipboard_text(sample)
            got = winapi.get_clipboard_text()
        finally:
            if previous is not None:
                winapi.set_clipboard_text(previous)
        if (got or "").replace("\r\n", "\n") != sample:
            raise core.ScreenFixError(f"Из буфера вернулось другое: {got!r}")
        return "запись и чтение текста (кириллица, переносы строк) работают"

    def hotkeys() -> str:
        if not IS_WIN:
            raise Skip("только Windows")
        if not hotkeys_free:
            raise Skip("клавиши сейчас занимает запущенная ScreenFix")
        from . import winapi

        cfg = cfg_holder.get("cfg") or core.load_config(directory, create=False)
        combos = core.parse_all_hotkeys(cfg)
        q: "queue.Queue[str]" = queue.Queue()
        try:
            t = winapi.start_hotkeys(combos, q)
        except core.ScreenFixError as e:
            raise core.ScreenFixError(str(e) + " (если ScreenFix уже запущена, закройте её перед проверкой)")
        t.stop()
        t.join(2.0)
        return "все четыре горячие клавиши свободны и регистрируются"

    def tk_windows() -> str:
        if not gui:
            raise Skip("пропущено внутри работающей программы")
        import tkinter as tk

        from . import ui

        root = tk.Tk()
        try:
            rw = ui.ResultWindow(root, lambda: None, lambda: None, lambda: None)
            fix = core.FixResult("Тут ашибка", "Тут ошибка", "test", 0.1, 0.2)
            rw.show_status("проверка")
            rw.show_stream("Тут ошиб")
            rw.show_result(fix, "")
            root.update()
            shown = rw.text.get("1.0", "end-1c")
            rw.hide()
            if "ошибка" not in shown:
                raise core.ScreenFixError(f"В окне результата не тот текст: {shown!r}")
        finally:
            root.destroy()
        return "окно результата создаётся и показывает правки"

    def copy_in_tk() -> str:
        if not IS_WIN:
            raise Skip("только Windows")
        if not gui:
            raise Skip("выполняется отдельно, из главного окна программы")
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        try:
            return copy_probe(root)
        finally:
            root.destroy()

    def ocr() -> str:
        if not IS_WIN:
            raise Skip("только Windows")
        from PIL import Image, ImageDraw, ImageFont

        from . import ocr_win

        cfg = cfg_holder.get("cfg") or core.load_config(directory, create=False)
        fonts = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Fonts")
        font = None
        for name in ("segoeui.ttf", "arial.ttf", "tahoma.ttf", "calibri.ttf"):
            p = os.path.join(fonts, name)
            if os.path.isfile(p):
                font = ImageFont.truetype(p, 34)
                break
        if font is None:
            raise Skip("в Windows не найден подходящий шрифт для тестовой картинки")
        img = Image.new("RGB", (900, 150), "white")
        draw = ImageDraw.Draw(img)
        draw.text((20, 20), "Проверка распознавания текста", fill="black", font=font)
        draw.text((20, 80), "Screen text recognition test", fill="black", font=font)
        lang = str(cfg["ocr_language"])
        try:
            text, used = ocr_win.recognize(img, lang, strict=bool(lang))
        except core.ScreenFixError as e:
            try:
                text, used = ocr_win.recognize(img, "en-US", strict=False)
            except core.ScreenFixError:
                raise core.ScreenFixError(str(e))
            raise Warn(f"Для языка «{lang}» распознавания нет: {e} Английский движок сработал и прочитал: {text!r}")
        low = text.lower()
        hits = sum(1 for w in ("проверка", "распознавания", "текста") if w in low)
        if hits < 2:
            raise Warn(f"Движок ({used}) работает, но прочитал с ошибками: {text!r}")
        return f"язык распознавания {used}; прочитано: {text.replace(chr(10), ' / ')!r}"

    _run(report, "Система", info)
    _run(report, "Настройки и правила", config_and_rules)
    _run(report, "Ядро (на заглушке LM Studio)", core_with_mock)
    _run(report, "LM Studio: сервер и модель", lm_models)
    _run(report, "LM Studio: пробное исправление", lm_fix)
    _run(report, "Буфер обмена", clipboard)
    _run(report, "Горячие клавиши Windows", hotkeys)
    _run(report, "Окна (tkinter)", tk_windows)
    _run(report, COPY_STEP_NAME, copy_in_tk)
    _run(report, "Чтение текста с экрана (OCR)", ocr)
    return report


def format_report(report: List[Result]) -> str:
    lines = []
    for r in report:
        lines.append(f"[{r.status}] {r.name}" + (f" — {r.detail}" if r.detail else ""))
    fails = sum(1 for r in report if r.status == "FAIL")
    warns = sum(1 for r in report if r.status == "WARN")
    lines.append("")
    lines.append(f"Итого: ошибок {fails}, предупреждений {warns}.")
    return "\n".join(lines)


def main(args: List[str]) -> int:
    out_path = None
    if "--out" in args:
        i = args.index("--out")
        out_path = args[i + 1] if i + 1 < len(args) else None
    directory = core.app_dir()
    report = run_all(directory, gui="--no-gui" not in args)
    text = format_report(report)
    try:
        print(text)
    except (OSError, ValueError, UnicodeEncodeError):
        pass  # у окна без консоли stdout может отсутствовать или быть в узкой кодировке
    path = out_path or os.path.join(directory, "selftest.txt")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError:
        pass
    failed = any(r.status == "FAIL" for r in report)
    if IS_WIN and "--quiet" not in args and sys.stdout is None:
        from . import winapi

        winapi.message_box("ScreenFix: самопроверка", text[-1800:] + f"\n\nОтчёт сохранён: {path}", 0x10 if failed else 0x40)
    return 1 if failed else 0
