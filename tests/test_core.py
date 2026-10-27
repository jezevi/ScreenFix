import json
import os
import socket
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from screenfix import core  # noqa: E402
from screenfix.mock_lm import MockLM, fake_correct  # noqa: E402


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class PureFunctions(unittest.TestCase):
    def test_strip_think_closed_block(self):
        self.assertEqual(core.strip_think("<think>абв</think>\n\nОтвет", final=True), "Ответ")

    def test_strip_think_orphan_closing_tag(self):
        self.assertEqual(core.strip_think("рассуждение\n</think>\n\nОтвет", final=True), "Ответ")

    def test_strip_think_unclosed_means_still_thinking(self):
        self.assertEqual(core.strip_think("Привет <think>думаю"), "Привет ")
        self.assertEqual(core.strip_think("<think>думаю"), "")

    def test_strip_think_plain_and_multiple(self):
        self.assertEqual(core.strip_think("Просто текст", final=True), "Просто текст")
        self.assertEqual(core.strip_think("<think>a</think>X<think>b</think>Y", final=True), "XY")

    def test_clean_input(self):
        self.assertEqual(core.clean_input("  a\r\nb \r\n", 100), "a\nb")
        with self.assertRaises(core.ScreenFixError):
            core.clean_input("   \n ", 100)
        with self.assertRaises(core.ScreenFixError) as cm:
            core.clean_input("x" * 11, 10)
        self.assertIn("max_input_chars", str(cm.exception))

    def test_parse_rules_ignores_comments(self):
        self.assertEqual(core.parse_rules("# заголовок\n1. Пункт один\n  # ещё комментарий\n2. Пункт два\n"), "1. Пункт один\n2. Пункт два")
        self.assertEqual(core.parse_rules(core.RULES_TEMPLATE), "")

    def test_build_messages(self):
        m = core.build_messages("1. Не использовать «ё».", "текст")
        self.assertEqual([x["role"] for x in m], ["system", "user"])
        self.assertIn("1. Не использовать «ё».", m[0]["content"])
        self.assertEqual(m[1]["content"], "текст")
        self.assertNotIn("распознаванием", m[0]["content"])
        self.assertIn("распознаванием", core.build_messages("", "т", ocr=True)[0]["content"])
        self.assertIn(core.RULES_EMPTY, core.build_messages("  ", "т")[0]["content"])

    def test_parse_hotkey(self):
        self.assertEqual(core.parse_hotkey("ctrl+alt+k"), (0x2 | 0x1, ord("K")))
        self.assertEqual(core.parse_hotkey("Ctrl+Shift+F9"), (0x2 | 0x4, 0x78))
        self.assertEqual(core.parse_hotkey("win+space"), (0x8, 0x20))
        for bad in ["k", "ctrl+", "ctrl+к", "ctrl+foo+k", "ctrl+f13"]:
            with self.assertRaises(core.ScreenFixError, msg=bad):
                core.parse_hotkey(bad)

    def test_parse_all_hotkeys(self):
        cfg = core.load_config(tempfile.mkdtemp(), create=False)
        self.assertEqual(set(core.parse_all_hotkeys(cfg)), set(core.HOTKEY_FIELDS))
        cfg["hotkey_paste"] = "Ctrl+Alt+K"  # совпадает с hotkey_fix
        with self.assertRaises(core.ScreenFixError) as cm:
            core.parse_all_hotkeys(cfg)
        self.assertIn("hotkey_paste", str(cm.exception))

    def test_diff_tokens_roundtrip(self):
        old = "Это ашибка в тексте, щас исправим."
        new = fake_correct(old)
        d = core.diff_tokens(old, new)
        self.assertEqual("".join(t for op, t in d if op in ("eq", "del")), old)
        self.assertEqual("".join(t for op, t in d if op in ("eq", "ins")), new)
        self.assertTrue(any(op == "ins" for op, _ in d))
        self.assertEqual(core.diff_tokens("так же", "так же"), [("eq", "так же")])

    def test_tidy_output(self):
        self.assertEqual(core.tidy_output("a", "```\nТекст\n```"), "Текст")
        self.assertEqual(core.tidy_output("a", "Исправленный текст:\nТекст"), "Текст")
        self.assertEqual(core.tidy_output("a", "«Текст»"), "Текст")
        # кавычки были в исходнике: не трогаем
        self.assertEqual(core.tidy_output("«Текст»", "«Текст»"), "«Текст»")
        # внутренние кавычки: не снимаем
        self.assertEqual(core.tidy_output("a", "«А» и «Б»"), "«А» и «Б»")
        self.assertEqual(core.tidy_output("a", "Обычный текст"), "Обычный текст")


class ConfigAndRules(unittest.TestCase):
    def test_defaults_and_file_creation(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = core.load_config(d)
            self.assertEqual(cfg["base_url"], "http://localhost:1234/v1")
            self.assertTrue(os.path.isfile(os.path.join(d, "config.json")))
            again = core.load_config(d)
            self.assertEqual(again, cfg)

    def test_override_and_unknown_keys(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "config.json"), "w", encoding="utf-8") as f:
                json.dump({"model": "qwen", "temperature": 0, "base_url": "http://x:1/v1/", "bogus": 1}, f)
            cfg = core.load_config(d)
            self.assertEqual(cfg["model"], "qwen")
            self.assertEqual(cfg["temperature"], 0.0)
            self.assertIsInstance(cfg["temperature"], float)
            self.assertEqual(cfg["base_url"], "http://x:1/v1")
            self.assertNotIn("bogus", cfg)

    def test_bad_config_is_a_readable_error(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            for content, needle in [("{не json", "config.json"), ('{"max_tokens": "много"}', "max_tokens"), ('{"stream": 1}', "stream"), ("[1]", "объект"), ('{"timeout_s": 0}', "больше нуля")]:
                with open(p, "w", encoding="utf-8") as f:
                    f.write(content)
                with self.assertRaises(core.ScreenFixError, msg=content) as cm:
                    core.load_config(d)
                self.assertIn(needle, str(cm.exception))

    def test_rules_file(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(core.load_rules(d), "")
            self.assertTrue(os.path.isfile(os.path.join(d, "rules.txt")))
            with open(os.path.join(d, "rules.txt"), "w", encoding="utf-8-sig") as f:
                f.write("# комментарий\nПравило 1\n")
            self.assertEqual(core.load_rules(d), "Правило 1")
            with open(os.path.join(d, "rules.txt"), "wb") as f:
                f.write("Правило".encode("cp1251"))
            with self.assertRaises(core.ScreenFixError) as cm:
                core.load_rules(d)
            self.assertIn("UTF-8", str(cm.exception))


class AgainstMockServer(unittest.TestCase):
    def setUp(self):
        self.mock = MockLM()
        self.url = self.mock.start()
        self.cfg = core.load_config(tempfile.mkdtemp(), create=False)
        self.cfg["base_url"] = self.url

    def tearDown(self):
        self.mock.stop()

    def test_pick_model_skips_embeddings(self):
        self.assertEqual(core.pick_model(self.cfg), "qwen3-8b")
        self.cfg["model"] = "my-model"
        self.assertEqual(core.pick_model(self.cfg), "my-model")
        self.cfg["model"] = ""
        self.mock.models = ["text-embedding-x"]
        with self.assertRaises(core.ScreenFixError) as cm:
            core.pick_model(self.cfg)
        self.assertIn("модель", str(cm.exception))

    def test_streaming_fix_with_inline_think_and_reasoning_field(self):
        self.mock.think = "inline"
        seen = []
        res = core.run_fix(self.cfg, "1. Правило", "Тут ашибка, щас исправим.", on_text=seen.append)
        self.assertEqual(res.corrected, "Тут ошибка, сейчас исправим.")
        self.assertTrue(res.changed)
        self.assertEqual(res.model, "qwen3-8b")
        self.assertTrue(all("это не должно попасть" not in s and "<think>" not in s for s in seen), seen)
        self.assertEqual(seen[-1], "Тут ошибка, сейчас исправим.")
        req = self.mock.requests[-1]
        self.assertTrue(req["stream"])
        self.assertEqual(req["temperature"], 0.1)
        self.assertIn("1. Правило", req["messages"][0]["content"])
        self.assertEqual(req["messages"][1]["content"], "Тут ашибка, щас исправим.")

    def test_orphan_closing_tag_across_chunk_boundaries(self):
        for chunk in (1, 2, 3, 5, 7):
            self.mock.think = "orphan"
            self.mock.chunk = chunk
            res = core.run_fix(self.cfg, "", "щас")
            self.assertEqual(res.corrected, "сейчас", chunk)

    def test_non_streaming(self):
        self.cfg["stream"] = False
        self.mock.think = "inline"
        res = core.run_fix(self.cfg, "", "Ашибка")
        self.assertEqual(res.corrected, "Ашибка".replace("ашибк", "ошибк"))  # заглавная «А» не совпала: текст без изменений
        self.assertFalse(self.mock.requests[-1]["stream"])

    def test_extra_body_is_sent(self):
        self.cfg["extra_body"] = {"top_p": 0.5}
        core.run_fix(self.cfg, "", "щас")
        self.assertEqual(self.mock.requests[-1]["top_p"], 0.5)

    def test_chatty_model_output_is_tidied(self):
        for wrap in ("fence", "label", "quotes"):
            self.mock.wrap = wrap
            res = core.run_fix(self.cfg, "", "Тут ашибка")
            self.assertEqual(res.corrected, "Тут ошибка", wrap)

    def test_no_changes_is_reported(self):
        res = core.run_fix(self.cfg, "", "Всё правильно.")
        self.assertFalse(res.changed)

    def test_http_error_message(self):
        self.mock.status = 500
        with self.assertRaises(core.ScreenFixError) as cm:
            core.run_fix(self.cfg, "", "текст")
        self.assertIn("500", str(cm.exception))

    def test_empty_answer_when_thinking_never_ends(self):
        self.mock.think = "unclosed"
        self.mock.finish = "length"
        with self.assertRaises(core.ScreenFixError) as cm:
            core.run_fix(self.cfg, "", "текст")
        self.assertIn("max_tokens", str(cm.exception))

    def test_server_down_message_mentions_lm_studio(self):
        self.cfg["base_url"] = f"http://127.0.0.1:{free_port()}/v1"
        with self.assertRaises(core.ScreenFixError) as cm:
            core.run_fix(self.cfg, "", "текст")
        self.assertIn("LM Studio", str(cm.exception))
        self.assertIn("сервер", str(cm.exception))

    def test_timeout_message(self):
        self.mock.delay = 0.5
        self.cfg["timeout_s"] = 0.2
        with self.assertRaises(core.ScreenFixError) as cm:
            core.run_fix(self.cfg, "", "текст длиннее")
        self.assertIn("замолчал", str(cm.exception))

    def test_connection_dropped_mid_answer(self):
        # «Сервер» присылает заголовки и половину куска, потом рвёт соединение.
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]

        def serve():
            c, _ = srv.accept()
            c.recv(65536)
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n10\r\ndata: {\"cho")
            c.close()

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        self.cfg["base_url"] = f"http://127.0.0.1:{port}/v1"
        self.cfg["model"] = "m"
        try:
            with self.assertRaises(core.ScreenFixError) as cm:
                core.run_fix(self.cfg, "", "текст")
        finally:
            srv.close()
        self.assertIn("оборвалось", str(cm.exception))

    def test_cancel_stops_stream(self):
        self.mock.delay = 0.05
        flag = {"stop": False}
        seen = []

        def on_text(t):
            seen.append(t)
            flag["stop"] = True

        with self.assertRaises(core.Cancelled):
            core.run_fix(self.cfg, "", "щас щас щас щас щас щас", on_text=on_text, should_cancel=lambda: flag["stop"])

    def test_too_long_input_never_reaches_server(self):
        self.cfg["max_input_chars"] = 5
        with self.assertRaises(core.ScreenFixError):
            core.run_fix(self.cfg, "", "слишком длинный текст")
        self.assertEqual(self.mock.requests, [])

    def test_system_proxy_is_ignored(self):
        # даже если в окружении задан прокси, к локальному серверу ходим напрямую
        old = {k: os.environ.get(k) for k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy")}
        try:
            for k in old:
                os.environ[k] = "http://127.0.0.1:9"
            res = core.run_fix(self.cfg, "", "щас")
            self.assertEqual(res.corrected, "сейчас")
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
