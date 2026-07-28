"""Ollama client — stdlib urllib only, no httpx, no ollama-python.

Uses /api/chat with `format` set to a JSON Schema so the model is constrained
to emit valid structured findings instead of prose we have to scrape.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass


class OllamaError(RuntimeError):
    pass


class OllamaUnavailable(OllamaError):
    pass


@dataclass
class ChatResult:
    content: str
    prompt_tokens: int = 0
    eval_tokens: int = 0
    duration_ms: int = 0
    truncated: bool = False


class OllamaClient:
    def __init__(
        self,
        host: str = "http://localhost:11434",
        model: str = "qwen2.5-coder:14b",
        num_ctx: int = 16384,
        temperature: float = 0.1,
        think: bool = False,
        timeout: int = 300,
    ) -> None:
        self.host = host.rstrip("/")
        self.model = model
        self.num_ctx = num_ctx
        self.temperature = temperature
        self.think = think
        self.timeout = timeout

    # -- transport -------------------------------------------------------

    def _post(self, path: str, payload: dict, timeout: int | None = None) -> dict:
        data = json.dumps(payload).encode()
        request = urllib.request.Request(
            f"{self.host}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")[:500]
            if exc.code == 404:
                raise OllamaError(
                    f"model '{self.model}' is not pulled. Run: ollama pull {self.model}"
                ) from exc
            raise OllamaError(f"ollama returned {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise OllamaUnavailable(
                f"cannot reach ollama at {self.host} ({exc.reason}). "
                "Is it running? Start it with: ollama serve"
            ) from exc
        except TimeoutError as exc:
            raise OllamaError(
                f"ollama timed out after {timeout or self.timeout}s — try a smaller model "
                "or raise [model] timeout"
            ) from exc

    def _get(self, path: str, timeout: int = 10) -> dict:
        try:
            with urllib.request.urlopen(f"{self.host}{path}", timeout=timeout) as response:
                return json.loads(response.read())
        except urllib.error.URLError as exc:
            raise OllamaUnavailable(
                f"cannot reach ollama at {self.host} ({exc.reason}). "
                "Is it running? Start it with: ollama serve"
            ) from exc

    # -- api -------------------------------------------------------------

    def list_models(self) -> list[str]:
        data = self._get("/api/tags")
        return sorted(m["name"] for m in data.get("models", []))

    def model_context_length(self, model: str | None = None) -> int | None:
        """The model's trained context length, so we can warn on a bad num_ctx."""
        try:
            data = self._post("/api/show", {"model": model or self.model}, timeout=20)
        except OllamaError:
            return None
        info = data.get("model_info", {}) or {}
        for key, value in info.items():
            if key.endswith(".context_length"):
                return int(value)
        return None

    def chat(
        self,
        system: str,
        user: str,
        schema: dict | None = None,
    ) -> ChatResult:
        payload: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {
                "temperature": self.temperature,
                "num_ctx": self.num_ctx,
            },
        }
        if schema is not None:
            payload["format"] = schema
        if self.think:
            payload["think"] = True

        data = self._post("/api/chat", payload)
        message = data.get("message", {}) or {}
        prompt_tokens = int(data.get("prompt_eval_count", 0) or 0)
        return ChatResult(
            content=message.get("content", "") or "",
            prompt_tokens=prompt_tokens,
            eval_tokens=int(data.get("eval_count", 0) or 0),
            duration_ms=int((data.get("total_duration", 0) or 0) / 1e6),
            # ollama drops the front of an oversized prompt silently; this is the tell
            truncated=prompt_tokens >= self.num_ctx - 8,
        )

    def health(self) -> tuple[bool, str]:
        try:
            models = self.list_models()
        except OllamaUnavailable as exc:
            return False, str(exc)
        if self.model not in models:
            base = self.model.split(":")[0]
            near = [m for m in models if m.split(":")[0] == base]
            if near:
                return True, f"model '{self.model}' not found; available tags: {', '.join(near)}"
            return False, (
                f"model '{self.model}' is not pulled. Run: ollama pull {self.model}\n"
                f"  available: {', '.join(models) or '(none)'}"
            )
        return True, f"ok — {len(models)} model(s), using {self.model}"
