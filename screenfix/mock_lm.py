"""Заглушка сервера LM Studio (OpenAI-совместимый /v1) для тестов и самопроверки. Модель не используется:
«исправление» — это простая подстановка, чтобы проверить весь путь запроса, стриминга и разбора ответа."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import List, Optional

# Что «исправляет» заглушка.
FIXES = [("ашибк", "ошибк"), ("щас", "сейчас"), ("коректор", "корректор")]


def fake_correct(text: str) -> str:
    for a, b in FIXES:
        text = text.replace(a, b)
    return text


class MockLM:
    """Параметры поведения можно менять между запросами:
    think: 'none' | 'inline' (<think>…</think>) | 'orphan' (рассуждение, потом </think>) | 'unclosed' (никогда не закрывается)
    status: код HTTP для /chat/completions (200 = норма)
    finish: 'stop' | 'length'
    models: список id для /models
    """

    def __init__(self) -> None:
        self.think = "none"
        self.status = 200
        self.finish = "stop"
        self.models: List[str] = ["text-embedding-nomic-embed-text-v1.5", "qwen3-8b"]
        self.delay = 0.0
        self.chunk = 3
        self.wrap = ""  # 'fence' | 'label' | 'quotes' — имитация «болтливой» модели
        self.requests: List[dict] = []
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # ── запуск ──
    def start(self) -> str:
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a, **k):  # тишина в тестах
                pass

            def _json(self, code: int, obj: dict) -> None:
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/v1/models":
                    self._json(200, {"data": [{"id": m, "object": "model"} for m in outer.models]})
                else:
                    self._json(404, {"error": "not found"})

            def do_POST(self):
                if self.path != "/v1/chat/completions":
                    return self._json(404, {"error": "not found"})
                n = int(self.headers.get("Content-Length", "0"))
                req = json.loads(self.rfile.read(n).decode("utf-8"))
                outer.requests.append(req)
                if outer.status != 200:
                    return self._json(outer.status, {"error": {"message": "mock failure"}})
                user = req["messages"][-1]["content"]
                fixed = fake_correct(user)
                if outer.wrap == "fence":
                    fixed = "```\n" + fixed + "\n```"
                elif outer.wrap == "label":
                    fixed = "Исправленный текст:\n" + fixed
                elif outer.wrap == "quotes":
                    fixed = "«" + fixed + "»"
                if outer.think == "inline":
                    full = "<think>\nПодумаю над текстом.\n</think>\n\n" + fixed
                elif outer.think == "orphan":
                    full = "Подумаю над текстом.\n</think>\n\n" + fixed
                elif outer.think == "unclosed":
                    full = "<think>\nДумаю, думаю, думаю…"
                else:
                    full = fixed
                if not req.get("stream"):
                    return self._json(
                        200, {"choices": [{"index": 0, "message": {"role": "assistant", "content": full}, "finish_reason": outer.finish}]}
                    )
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()

                def send(obj_or_str) -> None:
                    s = obj_or_str if isinstance(obj_or_str, str) else json.dumps(obj_or_str, ensure_ascii=False)
                    data = ("data: " + s + "\n\n").encode("utf-8")
                    self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                    self.wfile.flush()

                try:
                    # пустое служебное событие, как у реальных серверов
                    send({"choices": [{"index": 0, "delta": {"role": "assistant"}}]})
                    # отдельное поле reasoning_content клиент обязан игнорировать
                    send({"choices": [{"index": 0, "delta": {"reasoning_content": "это не должно попасть в текст"}}]})
                    for i in range(0, len(full), outer.chunk):
                        if outer.delay:
                            time.sleep(outer.delay)
                        send({"choices": [{"index": 0, "delta": {"content": full[i : i + outer.chunk]}}]})
                    send({"choices": [{"index": 0, "delta": {}, "finish_reason": outer.finish}]})
                    send("[DONE]")
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass  # клиент отменил запрос

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
