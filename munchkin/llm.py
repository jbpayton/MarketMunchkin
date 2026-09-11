"""LLM client over any OpenAI-compatible /v1/chat/completions server with tool calling:
LM Studio, Ollama, vLLM, OpenAI, OpenRouter, Anthropic's compatibility layer, or anything custom.
Provider, base URL, model and reasoning options live in settings (dashboard-editable); an optional
bearer key comes from .env (LLM_API_KEY) and is never exposed to the model."""
from __future__ import annotations

import json
import re
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .config import LOG_DIR, SETTINGS, LLMSettings, llm_api_key, load_llm_settings, redact

log = logging.getLogger("munchkin.llm")

if TYPE_CHECKING:
    from .tools import ToolRegistry

_RETRYABLE = (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError)


@dataclass
class LoopResult:
    final_text: str
    messages: list[dict[str, Any]]
    tool_calls: int
    steps: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    tool_log: list[dict[str, Any]] = field(default_factory=list)


_CTX_CACHE: dict[str, tuple[float, tuple[int | None, str]]] = {}
TOOL_OVERHEAD_TOKENS = 10_000      # ~48 tool schemas + prompt scaffolding
CHARS_PER_TOKEN = 4.0
MIN_RECOMMENDED_TOKENS = 32_768


def detect_context_tokens(s: LLMSettings) -> tuple[int | None, str]:
    """Ask the serving stack how big the context window is. Cached 10 minutes. (tokens or None, source)."""
    if s.context_tokens:
        return int(s.context_tokens), "configured (LLM_CONTEXT_TOKENS)"
    key = f"{s.provider}|{s.base_url}|{s.model}"
    hit = _CTX_CACHE.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    base = s.base_url.rstrip("/")
    root = base[:-3] if base.endswith("/v1") else base
    headers = {}
    k = llm_api_key()
    if k:
        headers["Authorization"] = f"Bearer {k}"
    res: tuple[int | None, str] = (None, "server does not report a context size")
    try:
        if s.provider == "lmstudio":
            for m in httpx.get(root + "/api/v0/models", timeout=5).json().get("data", []):
                if m.get("id") == s.model:
                    if m.get("loaded_context_length"):
                        res = (int(m["loaded_context_length"]), "LM Studio (loaded context)")
                    elif m.get("max_context_length"):
                        res = (int(m["max_context_length"]), "LM Studio (model max; not loaded)")
        elif s.provider == "ollama":
            d = httpx.post(root + "/api/show", json={"name": s.model}, timeout=5).json()
            m = re.search(r"num_ctx\s+(\d+)", d.get("parameters") or "")
            if m:
                res = (int(m.group(1)), "Ollama num_ctx")
            else:
                res = (4096, "Ollama default num_ctx (set num_ctx in a Modelfile or OLLAMA_CONTEXT_LENGTH)")
        elif s.provider == "openrouter":
            for m in httpx.get("https://openrouter.ai/api/v1/models", timeout=8).json().get("data", []):
                if m.get("id") == s.model and m.get("context_length"):
                    res = (int(m["context_length"]), "OpenRouter model card")
        elif s.provider == "anthropic":
            res = (200_000, "Anthropic (known)")
        elif s.provider == "openai":
            res = (128_000, "OpenAI (assumed)")
        else:  # vllm / custom: some servers expose max_model_len on /v1/models
            for m in httpx.get(base + "/v1/models", headers=headers, timeout=5).json().get("data", []):
                if m.get("id") == s.model:
                    n = m.get("max_model_len") or m.get("context_length") or m.get("context_window")
                    if n:
                        res = (int(n), "server /v1/models")
    except Exception as e:
        res = (None, f"detection failed: {type(e).__name__}")
    _CTX_CACHE[key] = (time.time(), res)
    return res


def context_plan(s: LLMSettings) -> dict[str, Any]:
    """Derive the transcript budget from the context window so a smaller model gets compacted harder instead of failing."""
    tokens, source = detect_context_tokens(s)
    plan: dict[str, Any] = {"tokens": tokens, "source": source, "configured_chars": s.context_char_budget,
                            "effective_chars": s.context_char_budget, "tool_result_chars": s.tool_result_max_chars, "warning": None}
    if tokens:
        usable = tokens - s.max_tokens - TOOL_OVERHEAD_TOKENS
        derived = max(12_000, int(usable * CHARS_PER_TOKEN))
        plan["effective_chars"] = min(s.context_char_budget, derived)
        scale = plan["effective_chars"] / max(1, s.context_char_budget)
        plan["tool_result_chars"] = max(800, int(s.tool_result_max_chars * min(1.0, scale)))
        if tokens < MIN_RECOMMENDED_TOKENS:
            plan["warning"] = f"context window {tokens:,} tokens is below the recommended {MIN_RECOMMENDED_TOKENS:,}; sessions will be compacted hard and lose detail"
    else:
        plan["warning"] = "context size unknown; using the configured budget — set LLM_CONTEXT_TOKENS if the server rejects long prompts"
    return plan


class LLMClient:
    def __init__(self, settings: LLMSettings | None = None):
        s = settings or load_llm_settings()
        self.s = s
        self.plan = context_plan(s)
        if self.plan.get("warning"):
            log.warning("LLM context: %s", self.plan["warning"])
        log.info("LLM context window %s (%s) -> budget %d chars", self.plan["tokens"], self.plan["source"], self.plan["effective_chars"])
        self.base = s.base_url.rstrip("/")
        self.model = s.model
        headers = {"Content-Type": "application/json"}
        key = llm_api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        self.client = httpx.Client(timeout=httpx.Timeout(s.timeout_s, connect=10.0), headers=headers)
        self._trace = open(LOG_DIR / "llm_trace.jsonl", "a", encoding="utf-8")

    # ------------------------------------------------------------------ low level
    def _trace_write(self, kind: str, payload: Any) -> None:
        try:
            self._trace.write(json.dumps({"ts": time.time(), "kind": kind, "data": payload}, default=str)[:20000] + "\n")
            self._trace.flush()
        except Exception:  # pragma: no cover
            pass

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=20),
           retry=retry_if_exception_type(_RETRYABLE), reraise=True)
    def chat(self, messages: list[dict[str, Any]], tools: list[dict] | None = None,
             tool_choice: str | None = None, max_tokens: int | None = None,
             temperature: float | None = None, reasoning_effort: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.s.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.s.max_tokens,
        }
        if self.s.send_reasoning_effort:
            payload["reasoning_effort"] = reasoning_effort or self.s.reasoning_effort
        if tools:
            payload["tools"] = tools
            if tool_choice:
                payload["tool_choice"] = tool_choice
        self._trace_write("request", {"n_messages": len(messages), "tools": bool(tools), "last": messages[-1]})
        r = self.client.post(f"{self.base}/v1/chat/completions", json=payload)
        if r.status_code >= 400:
            raise RuntimeError(f"LLM HTTP {r.status_code}: {redact(r.text[:500])}")
        data = r.json()
        choice = data["choices"][0]
        msg = choice["message"]
        usage = data.get("usage", {}) or {}
        self._trace_write("response", {"message": msg, "finish": choice.get("finish_reason"), "usage": usage})
        return {"message": msg, "finish_reason": choice.get("finish_reason"), "usage": usage}

    def simple(self, prompt: str, system: str | None = None, reasoning: str | None = None,
               max_tokens: int | None = None, temperature: float | None = None) -> str:
        """One-shot completion (no tools) through the same OpenAI-compatible endpoint."""
        msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        r = self.chat(msgs, max_tokens=max_tokens, temperature=temperature, reasoning_effort=reasoning)
        return (r["message"].get("content") or "").strip()

    def probe(self) -> dict[str, Any]:
        """Connectivity + tool-calling check used by the dashboard and `munchkin test-llm`."""
        t0 = time.time()
        r = self.chat([{"role": "user", "content": "Call the ping tool, then reply OK."}],
                      tools=[{"type": "function", "function": {"name": "ping", "description": "ping", "parameters": {"type": "object", "properties": {}}}}],
                      max_tokens=64, reasoning_effort="low")
        return {"ok": True, "model": self.model, "base_url": self.base, "tool_calls": bool(r["message"].get("tool_calls")),
                "reasoning_field": bool(r["message"].get("reasoning_content")), "secs": round(time.time() - t0, 1), "context": self.plan}

    # ------------------------------------------------------------------ context mgmt
    @staticmethod
    def _size(messages: list[dict[str, Any]]) -> int:
        n = 0
        for m in messages:
            n += len(m.get("content") or "")
            for tc in m.get("tool_calls") or []:
                n += len(json.dumps(tc))
        return n

    def _compact(self, messages: list[dict[str, Any]]) -> None:
        """Shrink old tool results when the transcript outgrows the budget."""
        budget = self.plan["effective_chars"]
        if self._size(messages) <= budget:
            return
        tool_idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
        keep_recent = 8
        for i in tool_idx[:-keep_recent] if len(tool_idx) > keep_recent else []:
            c = messages[i].get("content") or ""
            if len(c) > 500 and not c.endswith("[truncated]"):
                messages[i]["content"] = c[:400] + "\n...[truncated]"
            if self._size(messages) <= budget:
                return
        # Still too big: aggressively truncate everything but the last two tool results
        for i in tool_idx[:-2] if len(tool_idx) > 2 else []:
            c = messages[i].get("content") or ""
            if len(c) > 200:
                messages[i]["content"] = c[:160] + "\n...[truncated]"
        if self._size(messages) > budget:
            log.warning("context still over budget after compaction: %d chars", self._size(messages))

    # ------------------------------------------------------------------ tool loop
    def run_tool_loop(self, system: str, user: str, registry: "ToolRegistry",
                      max_calls: int | None = None,
                      on_event: Callable[[str, dict[str, Any]], None] | None = None) -> LoopResult:
        max_calls = max_calls or self.s.max_tool_calls
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        specs = registry.openai_specs()
        calls = 0
        steps = 0
        ptok = ctok = rtok = 0
        tool_log: list[dict[str, Any]] = []
        budget_exhausted = False
        cutoffs = 0
        emit = on_event or (lambda k, d: None)

        while steps < max_calls + 6:
            steps += 1
            self._compact(messages)
            try:
                resp = self.chat(messages, tools=specs, tool_choice="none" if budget_exhausted else None)
            except RuntimeError as e:
                if "exceed_context_size" not in str(e) and "context size" not in str(e):
                    raise
                # Over the server's window: shrink hard (keep only the last 2 tool results in full) and retry once.
                log.warning("context overflow reported by server; compacting aggressively")
                for m in messages:
                    if m.get("role") == "tool" and len(m.get("content") or "") > 300:
                        m["content"] = (m["content"][:240] + "\n...[truncated: context overflow]")
                resp = self.chat(messages, tools=specs, tool_choice="none" if budget_exhausted else None)
            msg = resp["message"]
            u = resp.get("usage") or {}
            ptok = u.get("prompt_tokens", ptok)
            ctok += u.get("completion_tokens", 0)
            rtok += (u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)
            assistant: dict[str, Any] = {"role": "assistant", "content": msg.get("content") or ""}
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls and not assistant["content"].strip():
                # Cut off (finish_reason=length while reasoning) or empty reply: nudge and retry with less thinking.
                cutoffs += 1
                if msg.get("reasoning_content"):
                    emit("reasoning", {"text": msg["reasoning_content"]})
                if cutoffs <= 2:
                    log.warning("empty assistant turn (finish=%s); nudging", resp.get("finish_reason"))
                    messages.append({"role": "user", "content": "Your last reply was cut off before any answer (too much internal deliberation). Decide now and reply directly and concisely, or call the next tool."})
                    resp = self.chat(messages, tools=specs, tool_choice="none" if budget_exhausted else None,
                                     reasoning_effort="low", max_tokens=self.s.max_tokens)
                    msg = resp["message"]
                    assistant = {"role": "assistant", "content": msg.get("content") or ""}
                    tool_calls = msg.get("tool_calls") or []
            if tool_calls:
                assistant["tool_calls"] = tool_calls
            messages.append(assistant)
            if msg.get("reasoning_content"):
                emit("reasoning", {"text": msg["reasoning_content"]})
            if assistant["content"]:
                emit("assistant", {"text": assistant["content"]})

            if not tool_calls:
                return LoopResult(final_text=assistant["content"], messages=messages, tool_calls=calls,
                                  steps=steps, prompt_tokens=ptok, completion_tokens=ctok,
                                  reasoning_tokens=rtok, tool_log=tool_log)

            for tc in tool_calls:
                calls += 1
                name = tc.get("function", {}).get("name", "")
                raw = tc.get("function", {}).get("arguments", "") or "{}"
                started = time.time()
                if budget_exhausted:
                    result = "ERROR: tool budget exhausted. Write your final summary now."
                else:
                    try:
                        args = json.loads(raw) if isinstance(raw, str) else dict(raw)
                    except json.JSONDecodeError as e:
                        args = None
                        result = f"ERROR: arguments were not valid JSON ({e}). Raw: {raw[:300]}"
                    if args is not None:
                        result = registry.call(name, args)
                result = redact(result)
                cap = (registry.limit(name) if hasattr(registry, "limit") else None) or self.s.tool_result_max_chars
                scale = self.plan["effective_chars"] / max(1, self.s.context_char_budget)
                if scale < 1.0:
                    cap = max(600, int(cap * scale))
                if len(result) > cap:
                    result = result[:cap] + f"\n...[truncated at {cap} chars; ask for a narrower slice]"
                messages.append({"role": "tool", "tool_call_id": tc.get("id"), "content": result})
                entry = {"name": name, "args": raw[:6000], "result": result, "secs": round(time.time() - started, 2)}
                tool_log.append(entry)
                emit("tool", entry)
            if calls >= max_calls and not budget_exhausted:
                budget_exhausted = True
                messages.append({"role": "user", "content": "Tool-call budget exhausted. Do not call more tools. Write your final session summary now in the required format."})

        # Fallback if the model never produced a final answer
        last = next((m["content"] for m in reversed(messages) if m["role"] == "assistant" and m.get("content")), "")
        return LoopResult(final_text=last or "(no final summary produced)", messages=messages, tool_calls=calls,
                          steps=steps, prompt_tokens=ptok, completion_tokens=ctok, reasoning_tokens=rtok, tool_log=tool_log)
