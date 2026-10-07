from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from pi_assistant.config import AgentConfig, Config, EmbeddingsConfig, LLMConfig, MemoryConfig
from pi_assistant.llm import LLMClient
from pi_assistant.memory import Embedder, MemoryService, MemoryStore

DIMS = 256


class FakeEmbedder(Embedder):
    """Deterministic bag-of-words embeddings: texts sharing words end up close together."""

    def __init__(self) -> None:
        self.cfg = EmbeddingsConfig(model="fake-embed", dimensions=DIMS, query_prefix="", document_prefix="")
        self.calls = 0

    async def embed(self, texts, kind, titles=None):  # type: ignore[override]
        self.calls += 1
        out = []
        for text in texts:
            vec = [0.0] * DIMS
            for word in re.findall(r"[a-z0-9']+", text.lower()):
                if len(word) < 3:
                    continue
                h = int(hashlib.md5(word.encode()).hexdigest(), 16)
                vec[h % DIMS] += 1.0
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            out.append([x / norm for x in vec])
        return out

    async def close(self) -> None:
        pass


@pytest.fixture
def memory(tmp_path: Path) -> MemoryService:
    store = MemoryStore(tmp_path / "test.db", DIMS, "fake-embed")
    svc = MemoryService(store, FakeEmbedder(), MemoryConfig(recall_max_distance=0.9))
    yield svc
    store.close()


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        llm=LLMConfig(model="test-model", base_url="http://llm.test/v1"),
        agent=AgentConfig(assistant_name="Testy", user_name="George", timezone="Europe/London"),
        embeddings=EmbeddingsConfig(model="fake-embed", dimensions=DIMS),
        base_dir=tmp_path,
    )


def completion(content: str | None = None, tool_calls: list[tuple[str, dict[str, Any] | str]] | None = None) -> dict:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = [
            {
                "id": f"call_{i}",
                "type": "function",
                "function": {"name": name, "arguments": args if isinstance(args, str) else json.dumps(args)},
            }
            for i, (name, args) in enumerate(tool_calls)
        ]
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "test-model",
        "choices": [{"index": 0, "finish_reason": "tool_calls" if tool_calls else "stop", "message": message}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    }


class FakeLLMServer:
    """An OpenAI-compatible endpoint (via httpx.MockTransport) that replays scripted replies.

    ``model`` adds fields to the model's entry in /models, and ``status`` is what oMLX's
    /models/status answers (a 404 if None).
    """

    def __init__(
        self,
        replies: list[dict] | Callable[[dict], dict],
        model: dict[str, Any] | None = None,
        status: dict[str, Any] | None = None,
    ):
        self.replies = replies
        self.requests: list[dict] = []
        self.model = model or {}
        self.status = status

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            entry = {"id": "test-model", "object": "model", **self.model}
            return httpx.Response(200, json={"object": "list", "data": [entry]})
        if request.url.path.endswith("/models/status"):
            return httpx.Response(200, json=self.status) if self.status else httpx.Response(404, json={"error": "no"})
        body = json.loads(request.content)
        self.requests.append(body)
        if callable(self.replies):
            return httpx.Response(200, json=self.replies(body))
        if not self.replies:
            return httpx.Response(500, json={"error": {"message": "no more scripted replies"}})
        return httpx.Response(200, json=self.replies.pop(0))

    def client(self, cfg: LLMConfig) -> LLMClient:
        return LLMClient(cfg, http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.handler)))


def bag_of_words(text: str) -> list[float]:
    vec = [0.0] * DIMS
    for word in re.findall(r"[a-z0-9']+", text.lower()):
        if len(word) >= 3:
            vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % DIMS] += 1.0
    return vec


class FakeEmbeddingsServer:
    """An OpenAI-compatible /v1/embeddings server on a real port, for code that makes its own client.

    "fake-a" and "fake-b" are bag-of-words models that find the same things, but put words in different
    places, so their vectors don't match. "fake-noise" ignores meaning, "fake-nan" returns NaN, and any
    other model isn't found.
    """

    def __init__(self) -> None:
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                server.requests.append(body)
                model, texts = body["model"], body["input"]
                if model not in {"fake-a", "fake-b", "fake-noise", "fake-nan"}:
                    return self._send(404, {"error": {"message": f'model "{model}" not found, try pulling it first'}})
                vectors = [server.vector(model, t) for t in texts]
                data = [{"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vectors)]
                self._send(
                    200,
                    {"object": "list", "data": data, "model": model, "usage": {"prompt_tokens": 0, "total_tokens": 0}},
                )

            def _send(self, status: int, payload: dict) -> None:
                raw = json.dumps(payload).encode()  # NaN goes out as NaN, as a broken server's would
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args: Any) -> None:
                pass

        self.requests: list[dict] = []
        self._http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._http.server_address[1]}/v1"
        threading.Thread(target=self._http.serve_forever, daemon=True).start()

    @staticmethod
    def vector(model: str, text: str) -> list[float]:
        if model == "fake-nan":
            return [float("nan")] * DIMS
        if model == "fake-noise":
            return bag_of_words(hashlib.md5(text.encode()).hexdigest())
        vec = bag_of_words(text)
        vec = vec if any(vec) else [1.0] + [0.0] * (DIMS - 1)
        return vec if model == "fake-a" else vec[::-1]  # as good as fake-a, but its vectors don't match

    def close(self) -> None:
        self._http.shutdown()
        self._http.server_close()


@pytest.fixture
def embeddings_server():
    server = FakeEmbeddingsServer()
    yield server
    server.close()
