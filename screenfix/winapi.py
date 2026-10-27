"""Windows-специфика: горячие клавиши, буфер обмена, эмуляция Ctrl+C / Ctrl+V, масштаб экрана.

Модуль импортируется только в Windows. Все функции WinAPI описаны с явными типами аргументов: без этого на
64-битной Windows указатели и дескрипторы обрезаются до 32 бит и вызовы падают случайным образом.
"""
from __future__ import annotations

import sys

if sys.platform != "win32":  # pragma: no cover
    raise ImportError("screenfix.winapi работает только в Windows")

import ctypes
import ctypes.wintypes as wt
import queue
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from .core import ScreenFixError

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_ULONG_PTR = ctypes.c_size_t
_LRESULT = ctypes.c_ssize_t


def _sig(fn, restype, *argtypes):
    fn.restype = restype
    fn.argtypes = list(argtypes)


# ── буфер обмена ──
_sig(user32.OpenClipboard, wt.BOOL, wt.HWND)
_sig(user32.CloseClipboard, wt.BOOL)
_sig(user32.EmptyClipboard, wt.BOOL)
_sig(user32.GetClipboardData, wt.HANDLE, wt.UINT)
_sig(user32.SetClipboardData, wt.HANDLE, wt.UINT, wt.HANDLE)
_sig(user32.GetClipboardSequenceNumber, wt.DWORD)
_sig(kernel32.GlobalAlloc, wt.HGLOBAL, wt.UINT, ctypes.c_size_t)
_sig(kernel32.GlobalLock, ctypes.c_void_p, wt.HGLOBAL)
_sig(kernel32.GlobalUnlock, wt.BOOL, wt.HGLOBAL)
_sig(kernel32.GlobalFree, wt.HGLOBAL, wt.HGLOBAL)

# ── клавиши, сообщения, окна ──
_sig(user32.RegisterHotKey, wt.BOOL, wt.HWND, ctypes.c_int, wt.UINT, wt.UINT)
_sig(user32.UnregisterHotKey, wt.BOOL, wt.HWND, ctypes.c_int)
_sig(user32.GetMessageW, ctypes.c_int, ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT)
_sig(user32.PeekMessageW, wt.BOOL, ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT)
_sig(user32.PostThreadMessageW, wt.BOOL, wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM)
_sig(kernel32.GetCurrentThreadId, wt.DWORD)
_sig(user32.GetAsyncKeyState, ctypes.c_short, ctypes.c_int)
_sig(user32.GetCursorPos, wt.BOOL, ctypes.POINTER(wt.POINT))
_sig(user32.GetSystemMetrics, ctypes.c_int, ctypes.c_int)
_sig(user32.GetParent, wt.HWND, wt.HWND)
_sig(kernel32.CreateMutexW, wt.HANDLE, ctypes.c_void_p, wt.BOOL, wt.LPCWSTR)

_SetWindowLongPtr = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
_GetWindowLongPtr = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
_sig(_SetWindowLongPtr, _LRESULT, wt.HWND, ctypes.c_int, _LRESULT)
_sig(_GetWindowLongPtr, _LRESULT, wt.HWND, ctypes.c_int)

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
ERROR_HOTKEY_ALREADY_REGISTERED = 1409
ERROR_ALREADY_EXISTS = 183
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080

VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN = 0x10, 0x11, 0x12, 0x5B, 0x5C
VK_C, VK_V = 0x43, 0x56


# ── масштаб экрана, один экземпляр, размеры экрана ──

def set_dpi_aware() -> None:
    """Нужно вызвать до создания окон: иначе при масштабе 125–150 % координаты экрана и окон расходятся."""
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = wt.BOOL
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):  # PER_MONITOR_AWARE_V2
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
        return
    except (AttributeError, OSError):
        pass
    try:
        user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


def acquire_single_instance(name: str = "Local\\ScreenFix_single_instance") -> bool:
    """True — мы первые. False — программа уже запущена."""
    handle = kernel32.CreateMutexW(None, False, name)
    already = ctypes.get_last_error() == ERROR_ALREADY_EXISTS
    if handle:
        _KEEP.append(handle)  # держим дескриптор до конца процесса
    return not already


_KEEP: List[int] = []


def message_box(title: str, text: str, icon: int = 0x40) -> None:
    """Системное окно с сообщением; не требует tkinter (нужно для ошибок на старте). icon: 0x10 — ошибка, 0x40 — информация."""
    try:
        user32.MessageBoxW.argtypes = [wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.UINT]
        user32.MessageBoxW.restype = ctypes.c_int
        user32.MessageBoxW(None, text, title, icon)
    except OSError:
        pass


def virtual_screen() -> Tuple[int, int, int, int]:
    """(x, y, ширина, высота) всех мониторов вместе; x и y могут быть отрицательными."""
    return (
        user32.GetSystemMetrics(76),
        user32.GetSystemMetrics(77),
        user32.GetSystemMetrics(78),
        user32.GetSystemMetrics(79),
    )


def cursor_pos() -> Tuple[int, int]:
    p = wt.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def make_noactivate(tk_window_id: int) -> None:
    """Окно не забирает фокус у программы, из которой взят текст (иначе Ctrl+V ушёл бы не туда)."""
    hwnd = user32.GetParent(tk_window_id) or tk_window_id
    style = _GetWindowLongPtr(hwnd, GWL_EXSTYLE)
    _SetWindowLongPtr(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)


# ── буфер обмена ──

def clipboard_seq() -> int:
    return int(user32.GetClipboardSequenceNumber())


def _open_clipboard(retries: int = 25, delay: float = 0.02) -> bool:
    for _ in range(retries):
        if user32.OpenClipboard(None):
            return True
        time.sleep(delay)
    return False


def get_clipboard_text() -> Optional[str]:
    """Текст из буфера или None, если там не текст (картинка, файлы) либо пусто."""
    if not _open_clipboard():
        raise ScreenFixError("Буфер обмена занят другой программой. Повторите через секунду.")
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text: str) -> None:
    data = text.replace("\x00", "").replace("\r\n", "\n").replace("\n", "\r\n")
    buf = ctypes.create_unicode_buffer(data)
    size = ctypes.sizeof(buf)
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
    if not handle:
        raise ScreenFixError("Не хватило памяти, чтобы положить текст в буфер обмена.")
    ptr = kernel32.GlobalLock(handle)
    if not ptr:
        kernel32.GlobalFree(handle)
        raise ScreenFixError("Не удалось подготовить буфер обмена.")
    ctypes.memmove(ptr, buf, size)
    kernel32.GlobalUnlock(handle)
    if not _open_clipboard():
        kernel32.GlobalFree(handle)
        raise ScreenFixError("Буфер обмена занят другой программой. Повторите через секунду.")
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            raise ScreenFixError("Не удалось записать текст в буфер обмена.")
        # после успешного SetClipboardData память принадлежит системе: освобождать её нельзя
    finally:
        user32.CloseClipboard()


# ── эмуляция клавиш ──

# Поля заданы типами фиксированной ширины (DWORD = 32 бита, WORD = 16 бит): размер структур от платформы не зависит.
_U32, _U16, _I32 = ctypes.c_uint32, ctypes.c_uint16, ctypes.c_int32


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", _U16), ("wScan", _U16), ("dwFlags", _U32), ("time", _U32), ("dwExtraInfo", _ULONG_PTR)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", _I32), ("dy", _I32), ("mouseData", _U32), ("dwFlags", _U32), ("time", _U32), ("dwExtraInfo", _ULONG_PTR)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", _U32), ("wParamL", _U16), ("wParamH", _U16)]


class _INPUT_UNION(ctypes.Union):
    # MOUSEINPUT — самый большой член: без него sizeof(INPUT) неверный, и SendInput возвращает ошибку 87
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", _U32), ("u", _INPUT_UNION)]


_sig(user32.SendInput, wt.UINT, wt.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)
KEYEVENTF_KEYUP = 0x0002


def send_keys(events: List[Tuple[int, bool]]) -> None:
    """events: [(код клавиши, True=нажать / False=отпустить), …] — отправляются одним пакетом."""
    arr = (_INPUT * len(events))()
    for i, (vk, down) in enumerate(events):
        arr[i].type = 1  # INPUT_KEYBOARD
        arr[i].ki.wVk = vk
        arr[i].ki.dwFlags = 0 if down else KEYEVENTF_KEYUP
    sent = user32.SendInput(len(events), arr, ctypes.sizeof(_INPUT))
    if sent != len(events):
        raise ScreenFixError(f"Windows не принял нажатие клавиш (код {ctypes.get_last_error()}).")


def _is_down(vk: int) -> bool:
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def wait_modifiers_released(timeout: float = 1.5) -> None:
    """Горячая клавиша срабатывает, пока пользователь ещё держит Ctrl+Alt. Если сейчас отправить Ctrl+C,
    программа получит Ctrl+Alt+C. Ждём отпускания; если держат дольше — отпускаем клавиши программно."""
    mods = (VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(_is_down(vk) for vk in mods):
            return
        time.sleep(0.01)
    held = [vk for vk in mods if _is_down(vk)]
    if held:
        send_keys([(vk, False) for vk in held])
        time.sleep(0.03)


def copy_selection(timeout: float = 1.2) -> str:
    """Нажимает Ctrl+C в активной программе и возвращает скопированный текст."""
    wait_modifiers_released()
    before = clipboard_seq()
    send_keys([(VK_CONTROL, True), (VK_C, True), (VK_C, False), (VK_CONTROL, False)])
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if clipboard_seq() != before:
            for _ in range(6):  # некоторые программы меняют буфер в два приёма: даём дописать
                time.sleep(0.04)
                text = get_clipboard_text()
                if text:
                    return text
            raise ScreenFixError(
                "В буфер обмена попало не текст (картинка или файл). Выделите именно текст. "
                "Если текст нельзя выделить, используйте чтение области экрана."
            )
        time.sleep(0.02)
    raise ScreenFixError(
        "Не получилось скопировать выделенный текст. Проверьте, что текст выделен. Если программа запущена "
        "от имени администратора, а ScreenFix нет, Windows не пропускает нажатия клавиш. "
        "Если текст в принципе нельзя выделить, используйте чтение области экрана."
    )


def paste_text(text: str) -> None:
    """Кладёт текст в буфер и нажимает Ctrl+V в активной программе (замена выделенного фрагмента)."""
    wait_modifiers_released()
    set_clipboard_text(text)
    time.sleep(0.03)
    send_keys([(VK_CONTROL, True), (VK_V, True), (VK_V, False), (VK_CONTROL, False)])


# ── горячие клавиши ──

class HotkeyThread(threading.Thread):
    """Отдельный поток с собственным циклом сообщений: RegisterHotKey привязывает клавиши к потоку."""

    def __init__(self, combos: Dict[str, Tuple[int, int]], out: "queue.Queue[Any]") -> None:
        super().__init__(daemon=True, name="screenfix-hotkeys")
        self.combos = combos
        self.out = out
        self.ready = threading.Event()
        self.error: Optional[str] = None
        self.thread_id = 0

    def run(self) -> None:
        self.thread_id = int(kernel32.GetCurrentThreadId())
        msg = wt.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0x0400, 0x0400, 0)  # создаёт очередь сообщений потока
        ids: Dict[int, str] = {}
        for i, (name, (mods, vk)) in enumerate(self.combos.items(), start=1):
            if not user32.RegisterHotKey(None, i, mods | MOD_NOREPEAT, vk):
                err = ctypes.get_last_error()
                if err == ERROR_HOTKEY_ALREADY_REGISTERED:
                    self.error = (
                        f"Горячая клавиша для «{name}» уже занята другой программой. "
                        f"Измените «{name}» в config.json (например, ctrl+alt+j)."
                    )
                else:
                    self.error = f"Windows не дал зарегистрировать горячую клавишу «{name}» (код {err})."
                break
            ids[i] = name
        self.ready.set()
        if self.error:
            for i in ids:
                user32.UnregisterHotKey(None, i)
            return
        while True:
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r <= 0:  # 0 — WM_QUIT, -1 — ошибка
                break
            if msg.message == WM_HOTKEY:
                name = ids.get(int(msg.wParam))
                if name:
                    self.out.put(name)
        for i in ids:
            user32.UnregisterHotKey(None, i)

    def stop(self) -> None:
        if self.thread_id:
            user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)


def start_hotkeys(combos: Dict[str, Tuple[int, int]], out: "queue.Queue[Any]") -> HotkeyThread:
    t = HotkeyThread(combos, out)
    t.start()
    if not t.ready.wait(3.0):
        raise ScreenFixError("Не удалось запустить обработку горячих клавиш (нет ответа от Windows).")
    if t.error:
        raise ScreenFixError(t.error)
    return t
