"""
Grok Head-Injection Gateway (multi-token)
将 OpenAI 兼容请求转发到 cli-chat-proxy.grok.com，自动注入：
  - Authorization: Bearer <grok token>
  - x-grok-client-version: 0.1.202  (缺失会 426)
  - x-grok-client-identifier: grok-pager
  - User-Agent 兜底

支持多账号 token 自动轮换：
  - round-robin 顺序挑选 token
  - 收到 429 (限流) / 401 (token 失效) 时自动切换到下一个 token 重试
  - 可通过 --ban-seconds N 让 429 的 token 冷却 N 秒后再回到池子

用途：cc-switch 本地代理（127.0.0.1:15721）上游 / 任何不支持自定义头的 OpenAI 客户端。

用法：
  # 单 token
  python tools/grok_gateway.py --token-file data/cpa_auth/xai-xxx.json [--port 40200]
  python tools/grok_gateway.py --token <access_token>
  python tools/grok_gateway.py --token-env GROK_TOKEN

  # 多 token 自动切换（扫描目录下所有 xai-*.json / token 文本文件）
  python tools/grok_gateway.py --token-dir data/cpa_auth --port 40200
"""
import argparse
import http.client
import json
import os
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

DEFAULT_UPSTREAM = "https://cli-chat-proxy.grok.com/v1"
GROK_VERSION = "0.1.202"

_token_pool = []          # [(token, last_ban_ts, ban_seconds), ...]
_pool_lock = threading.Lock()
_pool_next = 0


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


def collect_tokens(token_dir):
    found = []
    for name in sorted(os.listdir(token_dir)):
        p = os.path.join(token_dir, name)
        if not os.path.isfile(p):
            continue
        try:
            if name.endswith(".json"):
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
                t = data.get("access_token")
                lb = data.get("email", name)
            else:
                with open(p, encoding="utf-8") as f:
                    t = f.read().strip()
                lb = name
            if t and t.startswith("ey"):
                found.append((t, lb))
        except Exception:
            continue
    return found


def pick_token():
    """选一个未冷却的 token（round-robin），返回 (token, label) 或 None。"""
    global _pool_next
    now = time.time()
    with _pool_lock:
        n = len(_token_pool)
        if n == 0:
            return None
        for _ in range(n):
            t, label, ban_until = _token_pool[_pool_next % n]
            _pool_next = (_pool_next + 1) % n
            if now >= ban_until:
                return (t, label)
        return (_token_pool[_pool_next % n][0], _token_pool[_pool_next % n][1])


def ban_token(token, seconds):
    with _pool_lock:
        for i, (t, label, _) in enumerate(_token_pool):
            if t == token:
                _token_pool[i] = (t, label, time.time() + seconds)
                print(f"[gateway] token {label} cooled down for {seconds}s", flush=True)
                return


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print(f"[gateway] {self.command} {self.path} <- {self.headers.get('User-Agent','')[:60]}", flush=True)

    def _proxy(self, method):
        path = self.path
        print(f"[gateway] req {method} {path} te={self.headers.get('Transfer-Encoding','-')} cl={self.headers.get('Content-Length','-')}", flush=True)
        for hk, hv in self.headers.items():
            print(f"[gateway]   H {hk}: {hv[:80]}", flush=True)
        if path.startswith("/v1/"):
            path = path[len("/v1"):]
        url = Handler.upstream + path
        parsed = urlsplit(url)
        assert parsed.scheme == "https", "upstream must be https"
        ctx = ssl.create_default_context()
        conn = http.client.HTTPSConnection(parsed.hostname, 443, context=ctx, timeout=60)
        hdrs = {k: v for k, v in self.headers.items()
                if k.lower() not in ("host", "content-length", "connection",
                                     "authorization", "x-grok-client-version",
                                     "x-grok-client-identifier", "user-agent")}
        hdrs["x-grok-client-version"] = GROK_VERSION
        hdrs["x-grok-client-identifier"] = "grok-pager"
        hdrs["User-Agent"] = f"grok-pager/{GROK_VERSION} (gateway)"
        body = None
        if "Content-Length" in self.headers:
            try:
                body = self.rfile.read(int(self.headers["Content-Length"]))
            except Exception:
                body = None
        try:
            te = self.headers.get("Transfer-Encoding", "").lower()
        except Exception:
            te = ""
        if body is None and te == "chunked":
            try:
                body = b""
                while True:
                    size_line = self.rfile.readline()
                    if not size_line:
                        break
                    size = int(size_line.split(b";")[0].strip(), 16)
                    if size == 0:
                        self.rfile.readline()
                        break
                    body += self.rfile.read(size)
                    self.rfile.readline()
            except Exception:
                body = None
        if body is not None:
            hdrs["Content-Length"] = str(len(body))
        print(f"[gateway] body read len={0 if body is None else len(body)}", flush=True)
        if body is not None and "/responses" in self.path:
            try:
                bj = json.loads(body)
                print(f"[gateway] fwd-model={bj.get('model')} stream={bj.get('stream')} keys={list(bj.keys())[:12]} tools={len(bj.get('tools') or [])}", flush=True)
            except Exception as e:
                print(f"[gateway] body not json: {e!r}", flush=True)

        used_tokens = set()
        status = None
        while True:
            picked = pick_token()
            if picked is None:
                self._reply(502, {"error": {"message": "no token available"}})
                conn.close()
                return
            token, label = picked
            if token in used_tokens and len(used_tokens) == len(_token_pool):
                break
            used_tokens.add(token)
            hdrs_ = dict(hdrs)
            hdrs_["Authorization"] = "Bearer " + token
            try:
                conn.request(method, parsed.path, body=body, headers=hdrs_)
                resp = conn.getresponse()
                status = resp.status
                if status in (429,):
                    ban_token(token, Handler.ban_seconds)
                    self._drain(resp)
                    continue
                if status in (401, 403):
                    resp.read()
                    ban_token(token, Handler.ban_seconds)
                    continue
                self.send_response(status)
                del_h = ("content-length", "connection", "transfer-encoding", "content-encoding")
                for k, v in resp.getheaders():
                    if k.lower() not in del_h:
                        self.send_header(k, v)
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                t_start = time.time()
                print(f"[gateway] -> upstream status {status} for {self.command} {self.path} token {label}", flush=True)
                resp_body = b""
                chunk = resp.read1(65536)
                while chunk:
                    resp_body += chunk
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    if b'[DONE]' in chunk:
                        print(f"[gateway] SSE [DONE] seen after {time.time()-t_start:.1f}s", flush=True)
                    if b'response.completed' in chunk:
                        print(f"[gateway] response.completed seen at {time.time()-t_start:.1f}s", flush=True)
                    chunk = resp.read1(65536)
                print(f"[gateway] upstream body EOF after {time.time()-t_start:.1f}s total, resp_rcvd={len(resp_body)}", flush=True)
                try:
                    with open(r'C:\Users\fr_li\AppData\Local\Temp\opencode\gw_last_body.bin', 'wb') as fb:
                        fb.write(resp_body)
                except Exception:
                    pass
                conn.close()
                return
            except Exception as e:
                print(f"[gateway] ERROR {self.command} {self.path}: {e!r}", flush=True)
                try:
                    self._reply(502, {"error": {"message": f"gateway error: {e}"}})
                except Exception:
                    pass
                conn.close()
                return
        self._reply(status or 502, {"error": {"message": "all tokens throttled or invalid"}})
        conn.close()

    def _drain(self, resp):
        try:
            while resp.read(65536):
                pass
        except Exception:
            pass

    def _reply(self, code, obj):
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(obj).encode())
        except Exception:
            pass

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
    ap.add_argument("--token-dir", default=None, help="目录：扫描所有含 access_token 的 json / 文本 token 文件")
    ap.add_argument("--port", type=int, default=40200)
    ap.add_argument("--upstream", default=DEFAULT_UPSTREAM)
    ap.add_argument("--ban-seconds", type=int, default=300, help="429/401 后该 token 冷却秒数（默认 300）")
    args = ap.parse_args()

    entries = []
    if args.token_dir and os.path.isdir(args.token_dir):
        for t, lb in collect_tokens(args.token_dir):
            entries.append((t, lb))
        print(f"[gateway] loaded {len(entries)} token(s) from {args.token_dir}", flush=True)
    if not entries:
        src = args.token or (open(args.token_file, encoding="utf-8").read() if args.token_file else None)
        if args.token_env:
            src = os.environ.get(args.token_env)
        if not src:
            print("no token source given (use --token/--token-file/--token-dir)", file=sys.stderr)
            sys.exit(2)
        entries.append((load_token(src), "single"))

    now = time.time()
    with _pool_lock:
        _token_pool.clear()
        _token_pool.extend((t, lb, 0.0) for t, lb in entries)

    Handler.upstream = args.upstream.rstrip("/")
    Handler.ban_seconds = args.ban_seconds
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"grok gateway listening on http://127.0.0.1:{args.port} -> {Handler.upstream}", flush=True)
    print(f"tokens: {[lb for _, lb in entries]}", flush=True)
    srv.serve_forever()

if __name__ == '__main__':
    main()
