"""
Local preview server for the g4f-lite Vercel deployment.

This is ONLY for local testing / the v0 preview. In production Vercel serves
`public/` as static files and runs `api/index.py` as a serverless function
automatically (see vercel.json) -- this script just glues them together so you
can try it on one port locally.

    python3 scripts/vercel_dev.py        # serves on http://localhost:3000

It reuses the exact same request handler from api/index.py, so behaviour
matches production.
"""

import importlib.util
import os
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUBLIC = os.path.join(ROOT, "public")
PORT = int(os.environ.get("PORT", "3000"))

# Load the production handler from api/index.py
spec = importlib.util.spec_from_file_location("api_index", os.path.join(ROOT, "api", "index.py"))
api_index = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api_index)
ApiHandler = api_index.handler


class Router(SimpleHTTPRequestHandler):
    """Serve static files from public/, delegate API routes to the g4f handler."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=PUBLIC, **kwargs)

    def _is_api(self):
        p = self.path.split("?", 1)[0]
        return p.startswith("/api") or p.startswith("/v1")

    def _delegate(self, verb):
        # Rebind this connection to the API handler without re-reading the socket.
        api = ApiHandler.__new__(ApiHandler)
        api.rfile = self.rfile
        api.wfile = self.wfile
        api.headers = self.headers
        api.path = self.path
        api.command = self.command
        api.request_version = self.request_version
        api.client_address = self.client_address
        api.server = self.server
        api.connection = self.connection
        api.requestline = self.requestline
        getattr(api, verb)()

    def do_GET(self):
        if self._is_api():
            self._delegate("do_GET")
        else:
            super().do_GET()

    def do_POST(self):
        if self._is_api():
            self._delegate("do_POST")
        else:
            self.send_error(404)

    def do_OPTIONS(self):
        if self._is_api():
            self._delegate("do_OPTIONS")
        else:
            self.send_error(404)

    def log_message(self, fmt, *args):
        sys.stderr.write("[dev] " + (fmt % args) + "\n")


if __name__ == "__main__":
    print(f"g4f-lite dev server on http://localhost:{PORT}  (Ctrl+C to stop)")
    HTTPServer(("0.0.0.0", PORT), Router).serve_forever()
