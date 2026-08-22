"""
Grok Head-Injection Gateway
将 OpenAI 兼容请求转发到 cli-chat-proxy.grok.com，自动注入：
  - Authorization: Bearer <grok token>
  - x-grok-client-version: 0.1.202  (缺失会 426)
  - x-grok-client-identifier: grok-pager
  - User-Agent 兜底

用途：cc-switch 本地代理（127.0.0.1:15721）上游 / 任何不支持自定义头的 OpenAI 客户端。

用法：
  python tools/grok_gateway.py --token-file data/cpa_auth/xai-xxx.json [--port 40200] [--upstream https://cli-chat-proxy.grok.com/v1]
  python tools/grok_gateway.py --token <直接传 access_token>
  python tools/grok_gateway.py --token-env GROK_TOKEN
"""
import argparse
import http.client
import json
import ssl
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

DEFAULT_UPSTREAM = "https://cli-chat-proxy.grok.com/v1"
GROK_VERSION = "0.1.202"


def load_token(arg):
    if arg.startswith("ey"):  # JWT 直接当 token
        return arg.strip()
    try:
        with open(arg, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("access_token"):
            token = data["access_token"]
            if token.startswith("ey"):
                return token
    except Exception:
        pass
    return arg.strip()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    upstream = None
    token = None

    def log_message(self, *a):
        pass

    def _proxy(self, method):
        path = self.path
        if path.startswith("/v1/"):
            path = path[len("/v1"):]
        url = self.upstream + path
        parsed = urlsplit(url)
        assert parsed.scheme == "https", "upstream must be https"
        ctx = ssl.create_default_context()
        conn = http.client.HTTPSConnection(parsed.hostname, 443, context=ctx, timeout=300)
        hdrs = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "content-length", "connection")}
        hdrs["Authorization"] = "Bearer " + self.token
        hdrs["x-grok-client-version"] = GROK_VERSION
        hdrs["x-grok-client-identifier"] = "grok-pager"
        hdrs["User-Agent"] = hdrs.get("User-Agent") or f"grok-pager/{GROK_VERSION} (gateway)"
        body = None
        if "Content-Length" in self.headers:
            try:
                body = self.rfile.read(int(self.headers["Content-Length"]))
            except Exception:
                body = None
        try:
            conn.request(method, parsed.path, body=body, headers=hdrs)
            resp = conn.getresponse()
            self.send_response(resp.status)
            del_h = ("content-length", "connection", "transfer-encoding", "content-encoding")
            for k, v in resp.getheaders():
                if k.lower() not in del_h:
                    self.send_header(k, v)
            self.end_headers()
            chunk = resp.read(65536)
            while chunk:
                self.wfile.write(chunk)
                self.wfile.flush()
                chunk = resp.read(65536)
        except Exception as e:
            try:
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": {"message": f"gateway error: {e}"}}).encode())
            except Exception:
                pass
        finally:
            conn.close()

    do_GET = lambda self: self._proxy("GET")
    do_POST = lambda self: self._proxy("POST")
    do_PUT = lambda self: self._proxy("PUT")
    do_DELETE = lambda self: self._proxy("DELETE")
    do_PATCH = lambda self: self._proxy("PATCH")
    do_OPTIONS = lambda self: self._proxy("OPTIONS")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", default=None)
    ap.add_argument("--token-file", default=None)
    ap.add_argument("--token-env", default=None)
    ap.add_argument("--port", type=int, default=40200)
    ap.add_argument("--upstream", default=DEFAULT_UPSTREAM)
    args = ap.parse_args()

    src = args.token or (open(args.token_file, encoding="utf-8").read() if args.token_file else None)
    if args.token_env:
        import os
        src = os.environ.get(args.token_env)
    if not src:
        print("no token source given", file=sys.stderr)
        sys.exit(2)
    Handler.token = load_token(src)
    Handler.upstream = args.upstream.rstrip("/")
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"grok gateway listening on http://127.0.0.1:{args.port} -> {Handler.upstream}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()