"""Чтение текста с картинки встроенным распознаванием Windows (Windows.Media.Ocr), без установки пакетов.

Работает через Windows PowerShell 5.1 (он есть в любой Windows 10/11 и умеет вызывать WinRT). Обмен с ним идёт
через временные файлы и переменные окружения: так кириллица в путях и в тексте не портится кодовыми страницами.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
from typing import Tuple

from .core import ScreenFixError

# Сценарий PowerShell. Только ASCII: кодировка сценария не имеет значения.
PS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$outPath = $env:SF_OUT
function Write-Result($obj) {
  $json = $obj | ConvertTo-Json -Compress
  [System.IO.File]::WriteAllText($outPath, $json, (New-Object System.Text.UTF8Encoding($false)))
}
try {
  Add-Type -AssemblyName System.Runtime.WindowsRuntime
  $null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
  $null = [Windows.Storage.Streams.RandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
  $null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
  $null = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType = WindowsRuntime]
  $null = [Windows.Foundation.IAsyncOperation`1, Windows.Foundation, ContentType = WindowsRuntime]
  $null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Foundation, ContentType = WindowsRuntime]
  $null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
  $null = [Windows.Globalization.Language, Windows.Foundation, ContentType = WindowsRuntime]

  $asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
  })[0]
  function Await($op, $type) {
    $task = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
    $null = $task.Wait(-1)
    $task.Result
  }

  $engine = $null
  if ($env:SF_LANG) {
    try {
      $lang = New-Object Windows.Globalization.Language($env:SF_LANG)
      if ([Windows.Media.Ocr.OcrEngine]::IsLanguageSupported($lang)) {
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
      }
    } catch { $engine = $null }
  }
  if ($null -eq $engine) {
    if ($env:SF_STRICT -eq '1') { Write-Result @{ error = 'NO_LANGUAGE' }; exit 0 }
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
  }
  if ($null -eq $engine) { Write-Result @{ error = 'NO_ENGINE' }; exit 0 }

  $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($env:SF_IN)) ([Windows.Storage.StorageFile])
  $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
  $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
  $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
  $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
  $lines = @($result.Lines | ForEach-Object { $_.Text })
  Write-Result @{ lang = $engine.RecognizerLanguage.LanguageTag; text = ($lines -join "`n") }
} catch {
  Write-Result @{ error = 'EXCEPTION'; detail = ($_ | Out-String) }
}
"""

CREATE_NO_WINDOW = 0x08000000
MAX_SIDE = 4000  # у OcrEngine есть предел размера картинки (MaxImageDimension), берём с запасом


def powershell_path() -> str:
    root = os.environ.get("SystemRoot", r"C:\Windows")
    return os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")


def prepare_image(image):
    """Мелкий текст распознаётся заметно хуже: небольшие области увеличиваем вдвое."""
    from PIL import Image

    img = image.convert("RGB")
    w, h = img.size
    if max(w, h) < 1200:
        img = img.resize((w * 2, h * 2), Image.LANCZOS)
    w, h = img.size
    if max(w, h) > MAX_SIDE:
        k = MAX_SIDE / float(max(w, h))
        img = img.resize((max(1, int(w * k)), max(1, int(h * k))), Image.LANCZOS)
    return img


def _explain(code: str, detail: str, language: str) -> str:
    if code in ("NO_LANGUAGE", "NO_ENGINE"):
        return (
            f"В Windows нет распознавания текста для языка «{language}». Откройте Параметры → Время и язык → Язык и регион, "
            "добавьте нужный язык (оставьте галочку про распознавание текста) и повторите. "
            "Либо выделяйте текст мышью и используйте обычный режим."
        )
    d = (detail or "").strip()
    return "Не удалось распознать текст с экрана." + (f" Подробности: {d[:500]}" if d else "")


def recognize(image, language: str = "ru", strict: bool = False, timeout: float = 40.0) -> Tuple[str, str]:
    """Возвращает (текст, язык распознавания). strict=True — не подменять язык языками из профиля пользователя."""
    if sys.platform != "win32":
        raise ScreenFixError("Чтение текста с экрана работает только в Windows.")
    ps = powershell_path()
    if not os.path.isfile(ps):
        raise ScreenFixError("Не найден Windows PowerShell 5.1 (нужен для чтения текста с экрана).")
    img = prepare_image(image)
    with tempfile.TemporaryDirectory(prefix="screenfix_") as tmp:
        png, out = os.path.join(tmp, "in.png"), os.path.join(tmp, "out.json")
        img.save(png)
        env = dict(os.environ, SF_IN=png, SF_OUT=out, SF_LANG=language or "", SF_STRICT="1" if strict else "0")
        encoded = base64.b64encode(PS_SCRIPT.encode("utf-16-le")).decode("ascii")
        cmd = [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded]
        try:
            proc = subprocess.run(
                cmd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                creationflags=CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            raise ScreenFixError(f"Распознавание текста не завершилось за {int(timeout)} с.")
        except OSError as e:
            raise ScreenFixError(f"Не удалось запустить PowerShell: {e}")
        if not os.path.isfile(out):
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()[:400]
            raise ScreenFixError("Распознавание текста не вернуло результата." + (f" PowerShell: {err}" if err else ""))
        try:
            with open(out, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            raise ScreenFixError(f"Не удалось прочитать результат распознавания: {e}")
    if not isinstance(data, dict):
        raise ScreenFixError("Распознавание вернуло непонятный ответ.")
    if data.get("error"):
        raise ScreenFixError(_explain(str(data["error"]), str(data.get("detail", "")), language))
    text = str(data.get("text") or "").strip()
    if not text:
        raise ScreenFixError("На выбранной области текст не найден. Выделите область точнее и повторите.")
    return text, str(data.get("lang") or "")
