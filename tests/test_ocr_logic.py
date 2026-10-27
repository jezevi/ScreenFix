"""Python-сторона OCR: PowerShell подменён. Сам WinRT-сценарий здесь не исполняется (нужна Windows): проверяется только
его текст на очевидные ошибки и то, как программа передаёт ему данные и разбирает ответ."""
import base64
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image  # noqa: E402

from screenfix import core, ocr_win  # noqa: E402


class FakePowerShell:
    def __init__(self, payload=None, write=True, raise_exc=None):
        self.payload, self.write, self.raise_exc = payload, write, raise_exc
        self.seen = {}

    def __call__(self, cmd, env=None, **kw):
        if self.raise_exc:
            raise self.raise_exc
        self.seen = {"cmd": cmd, "env": env, "kw": kw}
        with Image.open(env["SF_IN"]) as im:  # картинка должна существовать к моменту запуска
            self.seen["size"] = im.size
        if self.write:
            with open(env["SF_OUT"], "w", encoding="utf-8") as f:
                json.dump(self.payload, f, ensure_ascii=False)
        return subprocess.CompletedProcess(cmd, 0, b"", b"some stderr")


class OcrLogic(unittest.TestCase):
    def setUp(self):
        self.p1 = mock.patch.object(sys, "platform", "win32")
        self.p2 = mock.patch.object(ocr_win, "powershell_path", lambda: sys.executable)
        self.p1.start()
        self.p2.start()
        self.img = Image.new("RGB", (100, 50), "white")

    def tearDown(self):
        self.p1.stop()
        self.p2.stop()

    def run_with(self, fake, **kw):
        with mock.patch.object(ocr_win.subprocess, "run", fake):
            return ocr_win.recognize(self.img, **kw)

    def test_success_and_arguments(self):
        fake = FakePowerShell({"lang": "ru", "text": "Строка 1\nСтрока 2"})
        text, lang = self.run_with(fake, language="ru", strict=True)
        self.assertEqual((text, lang), ("Строка 1\nСтрока 2", "ru"))
        env = fake.seen["env"]
        self.assertEqual((env["SF_LANG"], env["SF_STRICT"]), ("ru", "1"))
        self.assertEqual(fake.seen["size"], (200, 100))  # маленькая область увеличена вдвое
        cmd = fake.seen["cmd"]
        self.assertIn("-NoProfile", cmd)
        self.assertIn("-EncodedCommand", cmd)
        decoded = base64.b64decode(cmd[cmd.index("-EncodedCommand") + 1]).decode("utf-16-le")
        self.assertEqual(decoded, ocr_win.PS_SCRIPT)
        self.assertEqual(fake.seen["kw"]["creationflags"], ocr_win.CREATE_NO_WINDOW)
        self.assertEqual(fake.seen["kw"]["stdin"], subprocess.DEVNULL)

    def test_large_area_not_upscaled_and_capped(self):
        self.img = Image.new("RGB", (1500, 900))
        fake = FakePowerShell({"lang": "ru", "text": "x"})
        self.run_with(fake, language="ru")
        self.assertEqual(fake.seen["size"], (1500, 900))
        self.img = Image.new("RGB", (6000, 3000))
        self.run_with(fake, language="ru")
        self.assertEqual(fake.seen["size"], (4000, 2000))

    def test_non_strict_flag(self):
        fake = FakePowerShell({"lang": "en-US", "text": "x"})
        self.run_with(fake, language="en-US", strict=False)
        self.assertEqual(fake.seen["env"]["SF_STRICT"], "0")

    def test_missing_language_message(self):
        with self.assertRaises(core.ScreenFixError) as cm:
            self.run_with(FakePowerShell({"error": "NO_LANGUAGE"}), language="ru", strict=True)
        self.assertIn("«ru»", str(cm.exception))
        self.assertIn("Язык и регион", str(cm.exception))

    def test_exception_detail_is_passed_on(self):
        with self.assertRaises(core.ScreenFixError) as cm:
            self.run_with(FakePowerShell({"error": "EXCEPTION", "detail": "Cannot find type [Windows.Media.Ocr.OcrEngine]"}), language="ru")
        self.assertIn("Cannot find type", str(cm.exception))

    def test_no_output_file(self):
        with self.assertRaises(core.ScreenFixError) as cm:
            self.run_with(FakePowerShell(write=False), language="ru")
        self.assertIn("some stderr", str(cm.exception))

    def test_empty_text(self):
        with self.assertRaises(core.ScreenFixError) as cm:
            self.run_with(FakePowerShell({"lang": "ru", "text": "  "}), language="ru")
        self.assertIn("не найден", str(cm.exception))

    def test_timeout(self):
        with self.assertRaises(core.ScreenFixError) as cm:
            self.run_with(FakePowerShell(raise_exc=subprocess.TimeoutExpired("ps", 1)), language="ru", timeout=1)
        self.assertIn("не завершилось", str(cm.exception))

    def test_temp_files_are_removed(self):
        fake = FakePowerShell({"lang": "ru", "text": "x"})
        self.run_with(fake, language="ru")
        self.assertFalse(os.path.exists(fake.seen["env"]["SF_IN"]))


class PowerShellScriptStatic(unittest.TestCase):
    S = ocr_win.PS_SCRIPT

    def test_ascii_only(self):
        self.assertTrue(self.S.isascii())

    def test_brackets_balanced(self):
        for a, b in ("{}", "()", "[]"):
            self.assertEqual(self.S.count(a), self.S.count(b), a)

    def test_quotes_balanced_per_line(self):
        for n, line in enumerate(self.S.splitlines(), 1):
            stripped = line.replace("`n", "").replace("''", "")
            self.assertEqual(stripped.count("'") % 2, 0, f"строка {n}: {line}")
            self.assertEqual(stripped.count('"') % 2, 0, f"строка {n}: {line}")

    def test_uses_only_env_for_paths(self):
        for var in ("SF_IN", "SF_OUT", "SF_LANG", "SF_STRICT"):
            self.assertIn("$env:" + var, self.S)
        self.assertNotIn("{path}", self.S)

    def test_output_is_json_utf8_without_bom(self):
        self.assertIn("ConvertTo-Json", self.S)
        self.assertIn("UTF8Encoding($false)", self.S)


if __name__ == "__main__":
    unittest.main()
