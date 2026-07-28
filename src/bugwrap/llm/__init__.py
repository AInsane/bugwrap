"""Model backends. Ollama is the only one today; the surface is deliberately
small (`chat(system, user, schema) -> ChatResult`) so another backend is ~40 lines."""

from .ollama import ChatResult, OllamaClient, OllamaError, OllamaUnavailable

__all__ = ["OllamaClient", "ChatResult", "OllamaError", "OllamaUnavailable"]
