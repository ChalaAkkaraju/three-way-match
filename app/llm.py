"""Tool-calling chat, over the same two providers as extraction.

The agent needs something extraction did not: a conversation in which the
model asks for data and gets it back. Anthropic and OpenAI-shaped APIs
express that differently -- different content blocks, different names for the
same three things -- so the difference is isolated here and the agent above
sees one shape:

    ChatBackend.chat(messages, tools, system) -> ChatTurn(text, tool_calls)

`messages` is this module's own neutral format. Each backend translates on
the way out and normalises on the way back. Nothing above this file knows
which provider is answering, which is the same rule the extraction layer
follows and for the same reason: the provider is a delivery detail.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class LLMError(RuntimeError):
    pass


@dataclass
class ToolCall:
    call_id: str
    name: str
    arguments: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {"call_id": self.call_id, "name": self.name, "arguments": self.arguments}


@dataclass
class ChatTurn:
    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    raw_assistant: Any = None          # provider-shaped, for the next request
    usage: Dict[str, Any] = field(default_factory=dict)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: Dict[str, Any]         # JSON Schema for the arguments object


def _post(url: str, payload: Dict[str, Any], headers: Dict[str, str],
          timeout: int, who: str) -> Dict[str, Any]:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise LLMError(f"{who} returned {e.code}: {e.read().decode(errors='replace')[:400]}") from None
    except Exception as e:
        raise LLMError(f"could not reach {who}: {e}") from None


class ChatBackend:
    name = "base"
    model = ""

    def chat(self, messages: List[Dict[str, Any]], tools: List[ToolSpec],
             system: str, max_tokens: int = 2000) -> ChatTurn:
        raise NotImplementedError


# --------------------------------------------------------------------------
# Anthropic
# --------------------------------------------------------------------------


class AnthropicChat(ChatBackend):
    name = "anthropic"
    ENDPOINT = "https://api.anthropic.com/v1/messages"

    def __init__(self, model: Optional[str] = None, api_key: Optional[str] = None,
                 timeout: int = 120) -> None:
        self.model = model or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.timeout = timeout
        if not self.api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set")

    def _messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                out.append(m["raw"])
            elif m["role"] == "tool":
                out.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": r["call_id"],
                     "content": r["content"], "is_error": r.get("is_error", False)}
                    for r in m["results"]
                ]})
        return out

    def chat(self, messages, tools, system, max_tokens=2000) -> ChatTurn:
        body = _post(
            self.ENDPOINT,
            {
                "model": self.model,
                "max_tokens": max_tokens,
                "temperature": 0,
                "system": system,
                "tools": [{"name": t.name, "description": t.description,
                           "input_schema": t.parameters} for t in tools],
                "messages": self._messages(messages),
            },
            {"content-type": "application/json", "x-api-key": self.api_key,
             "anthropic-version": "2023-06-01"},
            self.timeout, "the Claude API",
        )
        text, calls = "", []
        for block in body.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")
            elif block.get("type") == "tool_use":
                calls.append(ToolCall(block["id"], block["name"], block.get("input") or {}))
        return ChatTurn(text=text.strip(), tool_calls=calls,
                        raw_assistant={"role": "assistant", "content": body.get("content", [])},
                        usage=body.get("usage") or {})


# --------------------------------------------------------------------------
# OpenRouter (OpenAI-shaped)
# --------------------------------------------------------------------------


class OpenRouterChat(ChatBackend):
    name = "openrouter"
    ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
    MODELS_ENDPOINT = "https://openrouter.ai/api/v1/models"
    FALLBACK_MODEL = "anthropic/claude-sonnet-4.5"

    def __init__(self, model: Optional[str] = None, api_key: Optional[str] = None,
                 timeout: int = 180) -> None:
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self.timeout = timeout
        if not self.api_key:
            raise LLMError("OPENROUTER_API_KEY is not set")
        self.model = model or os.environ.get("OPENROUTER_MODEL", "") or self._discover()

    def _discover(self) -> str:
        try:
            req = urllib.request.Request(
                self.MODELS_ENDPOINT, headers={"Authorization": f"Bearer {self.api_key}"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode()).get("data") or []
        except Exception:
            return self.FALLBACK_MODEL
        cands = [m.get("id", "") for m in data
                 if (m.get("id") or "").startswith("anthropic/") and ":" not in (m.get("id") or "")]
        for want in ("sonnet", "opus", "haiku"):
            hits = sorted((c for c in cands if want in c), reverse=True)
            if hits:
                return hits[0]
        return sorted(cands, reverse=True)[0] if cands else self.FALLBACK_MODEL

    def _messages(self, messages: List[Dict[str, Any]], system: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "user":
                text = m["content"] if isinstance(m["content"], str) else \
                    "\n".join(b.get("text", "") for b in m["content"])
                out.append({"role": "user", "content": text})
            elif m["role"] == "assistant":
                out.append(m["raw"])
            elif m["role"] == "tool":
                for r in m["results"]:
                    out.append({"role": "tool", "tool_call_id": r["call_id"],
                                "content": r["content"]})
        return out

    def chat(self, messages, tools, system, max_tokens=2000) -> ChatTurn:
        body = _post(
            self.ENDPOINT,
            {
                "model": self.model,
                "max_tokens": max_tokens,
                "temperature": 0,
                "messages": self._messages(messages, system),
                "tools": [{"type": "function",
                           "function": {"name": t.name, "description": t.description,
                                        "parameters": t.parameters}} for t in tools],
            },
            {"content-type": "application/json",
             "Authorization": f"Bearer {self.api_key}",
             "HTTP-Referer": "https://github.com/chalaakkaraju/three-way-match",
             "X-Title": "Invoice Match Desk"},
            self.timeout, "OpenRouter",
        )
        choices = body.get("choices") or []
        if not choices:
            raise LLMError(f"OpenRouter returned no completion: {str(body)[:300]}")
        msg = choices[0].get("message") or {}
        calls = []
        for tc in (msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(tc.get("id") or fn.get("name", ""), fn.get("name", ""), args))
        return ChatTurn(text=(msg.get("content") or "").strip(), tool_calls=calls,
                        raw_assistant=msg, usage=body.get("usage") or {})


# --------------------------------------------------------------------------

CHAT_BACKENDS = {
    "anthropic": ("ANTHROPIC_API_KEY", AnthropicChat),
    "openrouter": ("OPENROUTER_API_KEY", OpenRouterChat),
}


def available_chat_backend() -> Optional[str]:
    for name, (env, _) in CHAT_BACKENDS.items():
        if os.environ.get(env, "").strip():
            return name
    return None


def get_chat_backend(**kw) -> ChatBackend:
    name = available_chat_backend()
    if name is None:
        raise LLMError("No model API key is set. Set ANTHROPIC_API_KEY or OPENROUTER_API_KEY.")
    return CHAT_BACKENDS[name][1](**kw)
