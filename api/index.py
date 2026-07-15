"""
g4f-lite: a stdlib-only, serverless-friendly chat API for Vercel.

Why this exists
---------------
The full gpt4free (g4f) package depends on curl_cffi, fastapi, uvicorn, pystray,
headless browsers and long-lived sessions. None of that runs inside a Vercel
serverless function (size limits + no persistent process). This module is a
self-contained rewrite that:

  * uses ONLY the Python standard library (urllib/json) -> zero pip installs
  * talks to free, no-auth, OpenAI-compatible upstreams (Pollinations)
  * returns a rich `_debug` object on every call so failures are obvious
  * enables CORS so it works from a mobile browser (Kiwi) and Python alike

Routes (all handled by this single function via vercel.json rewrites):
  GET  /api/health                 -> liveness + upstream ping
  GET  /api/models                 -> available model ids
  GET  /api/providers              -> provider metadata
  POST /api/chat                   -> simple chat: {messages, model, provider}
  POST /v1/chat/completions        -> OpenAI-compatible alias
  GET  /v1/models                  -> OpenAI-compatible alias
"""

import json
import time
import uuid
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# --------------------------------------------------------------------------- #
# Provider configuration
# --------------------------------------------------------------------------- #
# Each provider must be reachable over plain HTTPS with no auth so it works on
# serverless. Pollinations exposes an OpenAI-compatible endpoint.
PROVIDERS = {
    "pollinations": {
        "label": "Pollinations AI",
        "url": "https://text.pollinations.ai/openai",
        "needs_auth": False,
        "models": [
            "openai",
            "openai-large",
            "openai-fast",
            "mistral",
            "llama",
            "deepseek",
            "qwen-coder",
        ],
    },
}

DEFAULT_PROVIDER = "pollinations"
DEFAULT_MODEL = "openai"
USER_AGENT = "g4f-lite/1.0 (+vercel-serverless)"
UPSTREAM_TIMEOUT = 45  # seconds


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _now_ms():
    return int(time.time() * 1000)


def _all_models():
    models = []
    for pid, meta in PROVIDERS.items():
        for m in meta["models"]:
            models.append({"id": m, "provider": pid})
    return models


def _resolve_provider(name):
    if not name:
        return DEFAULT_PROVIDER
    name = str(name).lower()
    return name if name in PROVIDERS else DEFAULT_PROVIDER


def _call_upstream(provider_id, model, messages, temperature, seed):
    """Call the upstream OpenAI-compatible endpoint. Returns (text, debug)."""
    meta = PROVIDERS[provider_id]
    url = meta["url"]
    payload = {
        "model": model,
        "messages": messages,
        "seed": seed if seed is not None else 42,
    }
    if temperature is not None:
        payload["temperature"] = temperature

    body = json.dumps(payload).encode("utf-8")
    debug = {
        "provider": provider_id,
        "model": model,
        "upstream_url": url,
        "request_payload": payload,
        "started_at": _now_ms(),
    }

    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )

    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            debug["upstream_status"] = resp.status
            debug["latency_ms"] = int((time.time() - t0) * 1000)
    except urllib.error.HTTPError as e:
        raw_err = e.read().decode("utf-8", errors="replace") if e.fp else ""
        debug["upstream_status"] = e.code
        debug["latency_ms"] = int((time.time() - t0) * 1000)
        debug["error"] = f"HTTP {e.code}: {e.reason}"
        debug["upstream_raw"] = raw_err[:2000]
        raise UpstreamError(f"upstream HTTP {e.code}: {e.reason}", debug)
    except urllib.error.URLError as e:
        debug["latency_ms"] = int((time.time() - t0) * 1000)
        debug["error"] = f"connection error: {e.reason}"
        raise UpstreamError(f"connection error: {e.reason}", debug)
    except Exception as e:  # noqa: BLE001
        debug["latency_ms"] = int((time.time() - t0) * 1000)
        debug["error"] = f"unexpected: {type(e).__name__}: {e}"
        raise UpstreamError(debug["error"], debug)

    debug["upstream_raw"] = raw[:2000]

    # Parse OpenAI-style JSON. Some free endpoints return plain text; handle both.
    text = None
    try:
        data = json.loads(raw)
        choices = data.get("choices") or []
        if choices:
            msg = choices[0].get("message") or {}
            text = msg.get("content")
            if text is None:
                text = choices[0].get("text")
        debug["upstream_parsed"] = "json"
        if data.get("usage"):
            debug["usage"] = data["usage"]
    except json.JSONDecodeError:
        text = raw
        debug["upstream_parsed"] = "text"

    if text is None or text == "":
        debug["error"] = "empty completion from upstream"
        raise UpstreamError("empty completion from upstream", debug)

    return text, debug


class UpstreamError(Exception):
    def __init__(self, message, debug):
        super().__init__(message)
        self.debug = debug


def do_chat(payload):
    """Run a chat request with model-level fallback. Returns response dict."""
    messages = payload.get("messages")
    if not messages:
        # allow a bare {"prompt": "..."} for quick testing
        prompt = payload.get("prompt")
        if prompt:
            messages = [{"role": "user", "content": str(prompt)}]
    if not messages or not isinstance(messages, list):
        return None, {
            "error": "invalid_request",
            "message": "`messages` (array) or `prompt` (string) is required",
        }, 400

    provider_id = _resolve_provider(payload.get("provider"))
    requested_model = payload.get("model") or DEFAULT_MODEL
    temperature = payload.get("temperature")
    seed = payload.get("seed")

    # Build the fallback chain: requested model first, then the rest.
    available = PROVIDERS[provider_id]["models"]
    chain = [requested_model] + [m for m in available if m != requested_model]

    attempts = []
    for model in chain:
        try:
            text, dbg = _call_upstream(provider_id, model, messages, temperature, seed)
            attempts.append({"model": model, "ok": True, "latency_ms": dbg.get("latency_ms")})
            response = {
                "id": "chatcmpl-" + uuid.uuid4().hex[:24],
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "provider": provider_id,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
                "_debug": {
                    "requested_model": requested_model,
                    "resolved_model": model,
                    "provider": provider_id,
                    "attempts": attempts,
                    "upstream": dbg,
                },
            }
            if dbg.get("usage"):
                response["usage"] = dbg["usage"]
            return response, None, 200
        except UpstreamError as e:
            attempts.append(
                {
                    "model": model,
                    "ok": False,
                    "error": str(e),
                    "latency_ms": e.debug.get("latency_ms"),
                    "upstream_status": e.debug.get("upstream_status"),
                }
            )
            continue

    # Every model failed.
    return None, {
        "error": "all_models_failed",
        "message": "Every model in the fallback chain failed. See attempts for details.",
        "provider": provider_id,
        "attempts": attempts,
    }, 502


def ping_upstream():
    """Lightweight health check against the default provider."""
    try:
        _, dbg = _call_upstream(
            DEFAULT_PROVIDER,
            DEFAULT_MODEL,
            [{"role": "user", "content": "ping"}],
            None,
            42,
        )
        return {"upstream": "reachable", "latency_ms": dbg.get("latency_ms")}
    except UpstreamError as e:
        return {"upstream": "unreachable", "error": str(e)}


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #
class handler(BaseHTTPRequestHandler):
    # -- utilities --------------------------------------------------------- #
    def _send(self, status, obj):
        payload = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _path(self):
        return urlparse(self.path).path.rstrip("/") or "/"

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    # -- verbs ------------------------------------------------------------- #
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = self._path()
        if path in ("/api/health", "/api/index", "/api"):
            self._send(200, {
                "status": "ok",
                "service": "g4f-lite",
                "time": int(time.time()),
                "default_provider": DEFAULT_PROVIDER,
                "default_model": DEFAULT_MODEL,
                **ping_upstream(),
            })
        elif path in ("/api/models", "/v1/models"):
            self._send(200, {"object": "list", "data": [
                {"id": m["id"], "object": "model", "owned_by": m["provider"]}
                for m in _all_models()
            ]})
        elif path == "/api/providers":
            self._send(200, {
                "object": "list",
                "data": [
                    {
                        "id": pid,
                        "label": meta["label"],
                        "needs_auth": meta["needs_auth"],
                        "models": meta["models"],
                    }
                    for pid, meta in PROVIDERS.items()
                ],
            })
        else:
            self._send(404, {"error": "not_found", "path": path,
                             "hint": "try /api/health, /api/models, /api/providers"})

    def do_POST(self):
        path = self._path()
        if path not in ("/api/chat", "/v1/chat/completions", "/api/index", "/api"):
            self._send(404, {"error": "not_found", "path": path,
                             "hint": "POST /api/chat"})
            return

        payload = self._read_json()
        if payload is None:
            self._send(400, {"error": "invalid_json",
                             "message": "request body is not valid JSON"})
            return

        response, error, status = do_chat(payload)
        if error is not None:
            self._send(status, error)
        else:
            self._send(status, response)

    # silence default logging to stderr (keeps Vercel logs clean, we log our own)
    def log_message(self, fmt, *args):  # noqa: A003
        return
