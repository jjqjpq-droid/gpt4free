# g4f-lite on Vercel

A serverless, OpenAI-compatible chat API + visual debug console, adapted from
this gpt4free repo so it actually runs on Vercel.

The full `g4f` package (curl_cffi, fastapi, uvicorn, headless browsers, pystray,
persistent sessions) **cannot** run on Vercel serverless. This deployment is a
self-contained, standard-library-only rewrite that talks to a free, no-auth,
OpenAI-compatible upstream (Pollinations). No API keys required.

## What ships to Vercel

| Path                     | What it is                                             |
|--------------------------|--------------------------------------------------------|
| `public/index.html`      | Debug console (served at `/`)                          |
| `api/index.py`           | Python serverless function (stdlib only)               |
| `vercel.json`            | Routing + rewrites + 60s function timeout              |
| `requirements.txt`       | Minimal (stdlib only) so the build stays tiny          |
| `requirements-full.txt`  | The original heavy g4f deps (for local / Docker only)  |
| `scripts/vercel_dev.py`  | Local one-port preview (not used in production)        |

## Deploy

1. Push this branch to GitHub (already connected).
2. In Vercel, import the repo (or it auto-deploys on push).
3. No environment variables needed. Done.

Vercel automatically serves `public/` as static and runs `api/index.py` as a
Python function. Open the deployed URL to get the debug console.

## Endpoints

Base URL = your Vercel domain (e.g. `https://your-app.vercel.app`).

| Method | Path                       | Description                          |
|--------|----------------------------|--------------------------------------|
| GET    | `/api/health`              | Liveness + upstream ping             |
| GET    | `/api/models`              | Available model ids                  |
| GET    | `/api/providers`           | Provider metadata                    |
| POST   | `/api/chat`                | Simple chat                          |
| POST   | `/v1/chat/completions`     | OpenAI-compatible alias              |
| GET    | `/v1/models`               | OpenAI-compatible alias              |

Every chat response includes a `_debug` object (upstream URL, HTTP status,
latency, raw payload, and per-model fallback attempts) so failures are obvious.

## curl (paste into Kiwi / terminal)

```bash
# health
curl https://your-app.vercel.app/api/health

# chat
curl -X POST https://your-app.vercel.app/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"Hello!"}]}'
```

In Kiwi browser: DevTools -> Network -> right-click the `/api/chat` request ->
"Copy as cURL" to grab a ready-made command for your own tool.

## Python client (for your Kiwi tool)

```python
import requests

BASE = "https://your-app.vercel.app"

def chat(prompt, model="openai", provider="pollinations"):
    r = requests.post(f"{BASE}/api/chat", json={
        "provider": provider,
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }, timeout=60)
    data = r.json()
    if "choices" in data:
        return data["choices"][0]["message"]["content"]
    raise RuntimeError(data)  # includes _debug / attempts

print(chat("Explain gpt4free in one line"))
```

Because the API is OpenAI-compatible you can also point the official `openai`
SDK at it:

```python
from openai import OpenAI
client = OpenAI(base_url="https://your-app.vercel.app/v1", api_key="not-needed")
print(client.chat.completions.create(
    model="openai",
    messages=[{"role": "user", "content": "hi"}],
).choices[0].message.content)
```

## Run locally

```bash
python3 scripts/vercel_dev.py     # http://localhost:3000
```

## Adding providers

Edit the `PROVIDERS` dict in `api/index.py`. Each entry needs a no-auth,
HTTPS, OpenAI-compatible `url` and a list of `models`. The fallback chain and
debug tracing work automatically for any provider you add.
```
