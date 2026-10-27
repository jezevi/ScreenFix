"""Запуск ScreenFix: панель управления, горячие клавиши, рабочие потоки."""
from __future__ import annotations

import os
import queue
import re
import sys
import threading
import time
import traceback
from typing import Callable, Dict, Optional

from . import core
from .core import Cancelled, ScreenFixError

LOG_NAME = "screenfix.log"
LOG_LIMIT = 200_000


def write_log(directory: str, message: str) -> None:
    """Короткий журнал рядом с программой. Сам текст сюда не пишется (если log_text не включён)."""
    path = os.path.join(directory, LOG_NAME)
    try:
        if os.path.isfile(path) and os.path.getsize(path) > LOG_LIMIT:
            with open(path, "rb") as f:
                f.seek(-LOG_LIMIT // 2, os.SEEK_END)
                tail = f.read()
            with open(path, "wb") as f:
                f.write(tail)
        with open(path, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + message.rstrip() + "\n")
    except OSError:
        pass


class Controller:
    def __init__(self, root, directory: str) -> None:
        from . import ocr_win, ui, winapi  # импорт здесь: модуль app должен импортироваться и без Windows (для тестов)

        self.ui, self.winapi, self.ocr = ui, winapi, ocr_win
        self.root = root
        self.dir = directory
        self.cfg = core.load_config(directory)
        core.load_rules(directory)  # создаёт rules.txt при первом запуске
        combos = core.parse_all_hotkeys(self.cfg)

        self.events: "queue.Queue[object]" = queue.Queue()
        self.busy = False
        self.cancel_event = threading.Event()  # у каждого запуска свой; отменённый поток больше ничего не меняет
        self.last_text: Optional[str] = None
        self.closing = False
        self._after_id = ""

        self.panel = ui.ControlPanel(
            root,
            {k: str(self.cfg[k]) for k in core.HOTKEY_FIELDS},
            {
                "check": self.check_server,
                "rules": lambda: self.open_file("rules.txt"),
                "config": lambda: self.open_file("config.json"),
                "selftest": self.run_selftest,
                "quit": self.quit,
            },
        )
        self.result = ui.ResultWindow(root, self.copy_result, self.paste_result, self.cancel)
        root.report_callback_exception = self._tk_error
        self.hotkeys = winapi.start_hotkeys(combos, self.events)  # бросает ScreenFixError, если клавиша занята
        self._after_id = root.after(40, self._poll)
        self.check_server()
        write_log(directory, f"Запущено, версия {core.VERSION}")

    # ── связь потоков с окнами ──
    def post(self, fn: Callable[[], None]) -> None:
        self.events.put(fn)

    def _poll(self) -> None:
        if self.closing:
            return
        try:
            while not self.closing:
                item = self.events.get_nowait()
                try:
                    if isinstance(item, str):
                        self.on_hotkey(item)
                    elif callable(item):
                        item()
                except Exception as e:  # ошибка в обработчике не должна ломать приём остальных событий
                    if not self.closing:
                        self._report_internal(e)
        except queue.Empty:
            pass
        if not self.closing:  # quit() мог уничтожить окно прямо внутри цикла выше
            self._after_id = self.root.after(40, self._poll)

    def _report_internal(self, e: BaseException) -> None:
        write_log(self.dir, "Внутренняя ошибка:\n" + "".join(traceback.format_exception(type(e), e, e.__traceback__)))
        self.busy = False
        self.result.show_error(f"Внутренняя ошибка: {e}. Подробности в файле {LOG_NAME} рядом с программой.")

    def _tk_error(self, exc, val, tb) -> None:
        self._report_internal(val if isinstance(val, BaseException) else exc(val))

    # ── горячие клавиши ──
    def on_hotkey(self, name: str) -> None:
        if name == "hotkey_quit":
            self.quit()
        elif name == "hotkey_fix":
            self.start_fix(ocr=False)
        elif name == "hotkey_ocr":
            self.start_fix(ocr=True)
        elif name == "hotkey_paste":
            self.paste_result()

    def start_fix(self, ocr: bool) -> None:
        if self.busy:
            self.result.set_busy_title("Ещё работаю над предыдущим текстом. Кнопка «Отмена» прервёт его.")
            return
        try:
            cfg = core.load_config(self.dir, create=False)  # конфиг и правила перечитываются каждый раз: правки действуют сразу
            rules = core.load_rules(self.dir, create=False)
        except ScreenFixError as e:
            self.result.show_error(str(e))
            return
        crop = None
        if ocr:
            self.busy = True  # пока выбираем область, повторные нажатия игнорируются
            try:
                self.result.hide()
                self.root.update()
                time.sleep(0.08)  # дать окну результата исчезнуть, чтобы оно не попало на снимок
                from PIL import ImageGrab

                x, y, _w, _h = self.winapi.virtual_screen()
                image = ImageGrab.grab(all_screens=True)
                box = self.ui.select_region(self.root, image, (x, y))
                if box is None:
                    self.busy = False
                    return
                crop = image.crop(box)
            except ScreenFixError as e:
                self.busy = False
                self.result.show_error(str(e))
                return
            except Exception as e:
                self.busy = False
                write_log(self.dir, "Снимок экрана не удался:\n" + traceback.format_exc())
                self.result.show_error(f"Не удалось сделать снимок экрана: {e}")
                return
        self.busy = True
        ev = threading.Event()
        self.cancel_event = ev
        self.result.show_status("Читаю текст с экрана…" if ocr else "Копирую выделенный текст…")
        threading.Thread(target=self._work, args=(ev, cfg, rules, ocr, crop), daemon=True, name="screenfix-work").start()

    def _finish(self, ev: threading.Event) -> None:
        if self.cancel_event is ev:  # более новый запуск не трогаем
            self.busy = False

    def _work(self, ev: threading.Event, cfg: Dict[str, object], rules: str, ocr: bool, crop) -> None:
        t0 = time.monotonic()
        previous_clip: Optional[str] = None

        def ui_post(fn: Callable[[], None]) -> None:
            if not ev.is_set():  # запуск отменён: окно больше не обновляем
                self.post(fn)

        try:
            if ocr:
                lang = str(cfg["ocr_language"])
                text, _used = self.ocr.recognize(crop, lang, strict=bool(lang))
            else:
                try:
                    previous_clip = self.winapi.get_clipboard_text()
                except ScreenFixError:
                    previous_clip = None
                text = self.winapi.copy_selection()
            if ev.is_set():
                raise Cancelled()

            latest: Dict[str, object] = {"text": None, "queued": False}

            def on_text(partial: str) -> None:  # приходит из рабочего потока; склеиваем частые обновления в одно
                latest["text"] = partial
                if not latest["queued"]:
                    latest["queued"] = True

                    def apply() -> None:
                        latest["queued"] = False
                        if latest["text"] is not None:
                            self.result.show_stream(str(latest["text"]))

                    ui_post(apply)

            fix = core.run_fix(
                cfg,
                rules,
                text,
                ocr=ocr,
                on_status=lambda m: ui_post(lambda m=m: self.result.set_busy_title(m)),
                on_text=on_text,
                should_cancel=ev.is_set,
            )
            if ev.is_set():  # отменили, пока шёл последний кусок: буфер и результат не трогаем
                raise Cancelled()
            note = ""
            try:
                if cfg["auto_copy"]:
                    self.winapi.set_clipboard_text(fix.corrected)
                    note = "скопировано в буфер"
                elif previous_clip is not None:
                    self.winapi.set_clipboard_text(previous_clip)  # вернуть то, что было в буфере до нас
            except ScreenFixError as e:
                note = f"в буфер не скопировано: {e}"
            self.last_text = fix.corrected
            chars = len(text)
            extra = f" текст={text!r} -> {fix.corrected!r}" if cfg["log_text"] else ""
            write_log(
                self.dir,
                f"ok режим={'экран' if ocr else 'выделение'} знаков={chars} модель={fix.model} "
                f"первый_ответ={fix.first_token_s if fix.first_token_s is None else round(fix.first_token_s, 2)}с "
                f"всего={round(time.monotonic() - t0, 2)}с изменён={fix.changed}{extra}",
            )
            ui_post(lambda: self.result.show_result(fix, note))
        except Cancelled:
            pass  # окно уже скрыто кнопкой «Отмена»
        except ScreenFixError as e:
            write_log(self.dir, f"ошибка: {e}")
            ui_post(lambda e=e: self.result.show_error(str(e)))
        except Exception as e:
            write_log(self.dir, "Внутренняя ошибка:\n" + traceback.format_exc())
            ui_post(lambda e=e: self.result.show_error(f"Внутренняя ошибка: {e}. Подробности в файле {LOG_NAME}."))
        finally:
            self.post(lambda: self._finish(ev))  # напрямую в очередь: освободить «занято» нужно в любом случае

    # ── кнопки окна результата ──
    def cancel(self) -> None:
        self.cancel_event.set()
        self.busy = False
        self.result.hide()

    def copy_result(self) -> None:
        if not self.last_text:
            return
        try:
            self.winapi.set_clipboard_text(self.last_text)
            self.result.title_var.set("Скопировано в буфер обмена")
        except ScreenFixError as e:
            self.result.show_error(str(e))

    def paste_result(self) -> None:
        """Вставляет исправленный текст на место выделенного: нажимает Ctrl+V в программе, где остался курсор."""
        if self.busy:
            return
        if not self.last_text:
            self.result.show_error("Пока нечего вставлять: сначала исправьте текст.")
            return
        text = self.last_text

        def run() -> None:
            try:
                self.winapi.paste_text(text)
                self.post(lambda: self.result.hide())
            except ScreenFixError as e:
                self.post(lambda e=e: self.result.show_error(str(e)))

        threading.Thread(target=run, daemon=True, name="screenfix-paste").start()

    # ── панель управления ──
    def check_server(self) -> None:
        self.panel.set_status("LM Studio: проверяю…")

        def run() -> None:
            try:
                cfg = core.load_config(self.dir, create=False)
                ids = core.list_models(cfg)
                chat_ids = [i for i in ids if not re.search(r"embed|rerank", i, re.I)]
                if cfg["model"] and cfg["model"] not in ids:
                    msg = f"LM Studio отвечает, но модели «{cfg['model']}» в списке нет. Доступны: {', '.join(chat_ids) or '—'}."
                    self.post(lambda: self.panel.set_status(msg, ok=False))
                elif not chat_ids and not cfg["model"]:
                    self.post(lambda: self.panel.set_status("LM Studio отвечает, но модели для чата не видно. Загрузите её в LM Studio.", ok=False))
                else:
                    name = cfg["model"] or chat_ids[0]
                    self.post(lambda: self.panel.set_status(f"LM Studio: подключено, модель {name}.", ok=True))
            except ScreenFixError as e:
                self.post(lambda e=e: self.panel.set_status(str(e), ok=False))

        threading.Thread(target=run, daemon=True, name="screenfix-check").start()

    def open_file(self, name: str) -> None:
        path = os.path.join(self.dir, name)
        try:
            if not os.path.isfile(path):
                core.load_rules(self.dir) if name == "rules.txt" else core.load_config(self.dir)
            os.startfile(path)  # type: ignore[attr-defined]  # только Windows
        except (OSError, ScreenFixError) as e:
            self.result.show_error(f"Не удалось открыть {name}: {e}")

    def run_selftest(self) -> None:
        self.panel.set_status("Самопроверка идёт…")

        def run() -> None:
            from . import selftest

            try:
                report = selftest.run_all(self.dir, gui=False, hotkeys_free=False)
            except Exception as e:
                self.post(lambda e=e: self.panel.set_status(f"Самопроверка не удалась: {e}", ok=False))
                return
            self.post(lambda: self._selftest_finish(report))

        threading.Thread(target=run, daemon=True, name="screenfix-selftest").start()

    def _selftest_finish(self, report) -> None:
        """Выполняется в главном потоке: проверке копирования нужно настоящее окно с фокусом."""
        from . import selftest

        extra = []
        selftest._run(extra, selftest.COPY_STEP_NAME, lambda: selftest.copy_probe(self.root))
        selftest.replace_result(report, extra[0])
        text = selftest.format_report(report)
        try:
            with open(os.path.join(self.dir, "selftest.txt"), "w", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            pass
        self.panel.set_status("Самопроверка закончена.", ok=not any(r.status == "FAIL" for r in report))
        self.ui.show_report(self.root, "ScreenFix: самопроверка", text)

    def quit(self) -> None:
        if self.closing:
            return
        self.closing = True
        self.cancel_event.set()
        try:
            self.hotkeys.stop()
        except Exception:
            pass
        write_log(self.dir, "Выход")
        try:
            self.root.after_cancel(self._after_id)
        except Exception:
            pass
        self.root.destroy()


def logging_flush() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None:
                stream.flush()
        except (OSError, ValueError):
            pass


def main(argv: Optional[list] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    directory = core.app_dir()
    if "--selftest" in args:
        from . import selftest

        return selftest.main(args)
    if sys.platform != "win32":
        print("ScreenFix работает только в Windows. Проверку ядра можно запустить так: python run.py --selftest")
        return 2
    from . import winapi

    try:
        winapi.set_dpi_aware()
        if not winapi.acquire_single_instance():
            winapi.message_box("ScreenFix", "ScreenFix уже запущен. Его окно можно найти на панели задач.")
            return 0
        import tkinter as tk

        root = tk.Tk()
        try:
            Controller(root, directory)
        except ScreenFixError as e:
            root.destroy()
            winapi.message_box("ScreenFix: не удалось запустить", str(e), 0x10)
            return 1
        root.mainloop()
        # Выходим сразу, не дожидаясь обычного завершения интерпретатора: иначе сборщик мусора может освободить
        # объекты tkinter из чужого потока, и Tcl аварийно завершает процесс (окно «программа не отвечает»).
        logging_flush()
        os._exit(0)
    except Exception:
        text = traceback.format_exc()
        try:
            with open(os.path.join(directory, "screenfix_crash.log"), "w", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            pass
        winapi.message_box("ScreenFix: сбой при запуске", "Программа не смогла запуститься. Подробности в файле screenfix_crash.log рядом с программой.\n\n" + text[-600:], 0x10)
        return 1
