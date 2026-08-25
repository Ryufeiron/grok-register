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
import base64
import hashlib
import http.client
import json
import os
import re
import socket
import ssl
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

# ---------- 极速优化：连接池 + SSL 上下文复用 + TCP_NODELAY ----------
_SSL_CTX = ssl.create_default_context()
_CONN_POOL = []                    # 空闲上游连接池（TLS keep-alive 复用）
_CONN_POOL_LOCK = threading.Lock()
_CONN_POOL_MAX = 16
_VERBOSE = bool(os.environ.get("GROK_GATEWAY_VERBOSE"))   # 逐头日志开关
_DUMP_BODY = bool(os.environ.get("GROK_GATEWAY_DUMP"))    # 响应落盘开关（调试用）


class _FastHTTPS(http.client.HTTPSConnection):
    """上游连接：TCP_NODELAY（消除 Nagle 40ms 延迟）。"""

    def connect(self):
        super().connect()
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass


def _acquire_conn(host):
    """从池中取空闲连接，没有则新建（省去重复 TLS 握手）。"""
    with _CONN_POOL_LOCK:
        while _CONN_POOL:
            c = _CONN_POOL.pop()
            if getattr(c, "host", None) == host:
                return c
            try:
                c.close()
            except Exception:
                pass
    return _FastHTTPS(host, 443, context=_SSL_CTX, timeout=180)


def _release_conn(conn):
    """响应读完的连接放回池复用；池满则关闭。"""
    try:
        with _CONN_POOL_LOCK:
            if len(_CONN_POOL) < _CONN_POOL_MAX:
                _CONN_POOL.append(conn)
                return
        conn.close()
    except Exception:
        pass


def _send_upstream(conn, host, method, path, body, hdrs):
    """发送请求；池中旧连接若已被上游断开则自动换新重发一次。"""
    try:
        conn.request(method, path, body=body, headers=hdrs)
        return conn, conn.getresponse()
    except (http.client.RemoteDisconnected, ConnectionResetError,
            BrokenPipeError, ssl.SSLError, OSError):
        try:
            conn.close()
        except Exception:
            pass
        conn = _FastHTTPS(host, 443, context=_SSL_CTX, timeout=180)
        conn.request(method, path, body=body, headers=hdrs)
        return conn, conn.getresponse()

DEFAULT_UPSTREAM = "https://cli-chat-proxy.grok.com/v1"
GROK_VERSION = "0.1.202"
CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
REFRESH_SCOPE = "openid profile email offline_access grok-cli:access api:access"
TOKEN_ENDPOINT = "https://auth.x.ai/oauth2/token"

_token_pool = []          # [(token, label, ban_until, refresh_token)]
_pool_lock = threading.Lock()
_pool_next = 0
_sticky_idx = -1          # Sticky: 当前粘性 token 的池索引（缓存连续性）
_quota_est = {}           # label -> {remaining, actual, limit, updated_at}
QUOTA_LIMIT = 500000
QUOTA_EXHAUSTED_THRESHOLD = 0
PROBE_MIN_INTERVAL = 1800  # 自愈探测最小间隔（秒），防止风暴反复锁死
QUOTA_STATE_FILE = None   # 额度状态持久化文件（main 设置，如 data/quota_state.json）
_quota_last_save = 0.0
QUOTA_SAVE_INTERVAL = 30.0  # quota state disk-flush throttle (seconds)
_REQ_TOOLS = {"names": [], "bash_name": None}   # 最近一次请求的工具名（多线程由 GIL 保护，可接受）
EMPTY_EDIT_LOOP_LIMIT = 3                        # 连续空 Edit 次数阈值，超过后解除 FORCED 破环
_empty_edit_streak = 0                           # 当前连续空 Edit 计数
DEGEN_FORCED_LIMIT = 4                           # FORCED fast-empty streak threshold before falling back to keep-client
_DEGEN_STREAK = {"n": 0}                         # consecutive FORCED degenerate fast-empty responses (global)
GREETING_RE = re.compile(
    r'^\s*(hi|hi there|hello|hey|yo|hey there|你好|您好|嗨|哈喽|哈啰|在吗|在么|再见|拜拜|bye|good night|good morning|晚安|早安)\s*[!！.。?？~～,，、…\-]*\s*$',
    re.IGNORECASE,
)
# 客户端（opencode 等）会在用户消息中注入 <env>/<instructions> 等包装块，匹配问候/确认前先剥离
CLIENT_WRAPPER_RE = re.compile(
    r'<\s*(?:env|environment|instructions|system-reminder|context|dcp-message-id|dcp-system-reminder)(?:\s[^>]*)?>.*?<\s*/\s*(?:env|environment|instructions|system-reminder|context|dcp-message-id|dcp-system-reminder)\s*>',
    re.DOTALL | re.IGNORECASE,
)


def strip_client_wrappers(text):
    try:
        cleaned = CLIENT_WRAPPER_RE.sub(" ", text or "")
    except Exception:
        return text or ""
    return cleaned.strip()


_DCP_TAG_RES = (
    re.compile(r'<dcp-message-id>[^<]{0,80}</dcp-message-id>', re.IGNORECASE),
    re.compile(r'<dcp-system-reminder>.*?</dcp-system-reminder>', re.DOTALL | re.IGNORECASE),
)


def _strip_dcp_tags(text):
    n = 0
    for pat in _DCP_TAG_RES:
        text, k = pat.subn('', text)
        n += k
    return text, n


def _sanitize_conversation(bj):
    """剥离会话所有消息里的 dcp 元数据标签，返回清洗数。"""
    total = 0
    conv = bj.get('messages') if isinstance(bj.get('messages'), list) else (bj.get('input') if isinstance(bj.get('input'), list) else None)
    if not conv:
        return 0
    for msg in conv:
        if not isinstance(msg, dict):
            continue
        c = msg.get('content')
        if isinstance(c, str):
            t, n = _strip_dcp_tags(c)
            if n:
                msg['content'] = t
                total += n
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and isinstance(part.get('text'), str):
                    t, n = _strip_dcp_tags(part['text'])
                    if n:
                        part['text'] = t
                        total += n
    return total
SMALL_TALK_RE = re.compile(
    r'^\s*(hi|hello|hey|yo|hey there|你好|您好|嗨|哈喽|哈啰|在吗|在么|谢谢|多谢|感谢|thanks|thank you|thx|ty|ok|okay|好的|好|明白|了解|收到|嗯|哦|哦哦|行|可以的|辛苦了|再见|拜拜|bye|good night|good morning|晚安|早安)\s*[!！.。?？~～,，、…\-]*\s*$',
    re.IGNORECASE,
)

# 运行统计（供控制台 /status 使用）
_stats = {
    "started_at": None,
    "requests_total": 0,
    "requests_429": 0,
    "requests_exhausted": 0,
    "token_exhausted_count": {},  # label -> count（额度耗尽）
    "requests_401": 0,
    "requests_ok": 0,
    "refresh_count": 0,
    "refresh_failed": 0,
    "empty_edit_filtered": 0,
    "token_last_used": {},   # label -> ts
    "token_429_count": {},   # label -> count
    "token_401_count": {},   # label -> count
    "token_ok_count": {},    # label -> count
}
_stats_lock = threading.Lock()


def stats_record(label, kind):
    """kind: ok / 429 / 401 / refresh / refresh_fail / empty_edit"""
    with _stats_lock:
        s = _stats
        if kind == "ok":
            s["requests_ok"] += 1
            s["requests_total"] += 1
            s["token_ok_count"][label] = s["token_ok_count"].get(label, 0) + 1
        elif kind == "429":
            s["requests_429"] += 1
            s["requests_total"] += 1
            s["token_429_count"][label] = s["token_429_count"].get(label, 0) + 1
        elif kind == "exhausted":
            s["requests_exhausted"] += 1
            s["requests_total"] += 1
            s["token_exhausted_count"][label] = s["token_exhausted_count"].get(label, 0) + 1
        elif kind == "401":
            s["requests_401"] += 1
            s["requests_total"] += 1
            s["token_401_count"][label] = s["token_401_count"].get(label, 0) + 1
        elif kind == "refresh":
            s["refresh_count"] += 1
        elif kind == "refresh_fail":
            s["refresh_failed"] += 1
        elif kind == "empty_edit":
            s["empty_edit_filtered"] += 1
        if label:
            s["token_last_used"][label] = time.time()


def snapshot_status():
    """返回完整状态 JSON（供控制端口 /status 与日志）。"""
    now = time.time()
    with _pool_lock:
        pool = list(_token_pool)
    with _stats_lock:
        stats = dict(_stats)
        last_used = dict(stats.pop("token_last_used", {}))
        c429 = dict(stats.pop("token_429_count", {}))
        c401 = dict(stats.pop("token_401_count", {}))
        cok = dict(stats.pop("token_ok_count", {}))
        cex = dict(stats.pop("token_exhausted_count", {}))
    tokens = []
    for t, label, ban_until, rt in pool:
        exp = _jwt_exp(t)
        expired = bool(exp and now >= exp)
        cooling = now < ban_until
        tokens.append({
            "label": label,
            "has_refresh": bool(rt),
            "expired": expired,
            "exp_ts": exp,
            "cooling": cooling,
            "cooldown_until": int(ban_until) if cooling else None,
            "last_used_ts": last_used.get(label),
            "count_429": c429.get(label, 0),
            "count_401": c401.get(label, 0),
            "count_ok": cok.get(label, 0),
            "count_exhausted": cex.get(label, 0),
            "quota_remaining": _quota_get(label),
            "quota_actual": (_quota_est.get(label) or {}).get("actual"),
            "quota_limit": (_quota_est.get(label) or {}).get("limit", QUOTA_LIMIT),
            "quota_updated_at": (_quota_est.get(label) or {}).get("updated_at"),
            "sticky": label == (_token_pool[_sticky_idx][1] if 0 <= _sticky_idx < len(_token_pool) else None),
            "bindings": _binding_count(label),
        })
    total_quota = sum(_quota_get(t[1]) for t in pool)
    return {
        "ok": True,
        "running": True,
        "started_at": stats["started_at"],
        "upstream": Handler.upstream if hasattr(Handler, "upstream") else DEFAULT_UPSTREAM,
        "port": Handler.port if hasattr(Handler, "port") else None,
        "tokens_total": len(tokens),
        "tokens_expired": sum(1 for t in tokens if t["expired"]),
        "tokens_cooling": sum(1 for t in tokens if t["cooling"]),
        "tokens_healthy": sum(1 for t in tokens if not t["expired"] and not t["cooling"]),
        "tokens_quota_total": total_quota,
        "empty_edit_streak": _empty_edit_streak,
        "empty_edit_loop_limit": EMPTY_EDIT_LOOP_LIMIT,
        "sessions_active": len([k for k, v in _session_ts.items() if time.time() - v <= SESSION_TTL]),
        "session_map": {k: v for k, v in _session_map.items()},
        "stats": stats,
        "tokens": tokens,
    }


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
                rt = data.get("refresh_token") or ""
            else:
                with open(p, encoding="utf-8") as f:
                    t = f.read().strip()
                lb = name
                rt = ""
            if t and t.startswith("ey"):
                found.append((t, lb, rt, p))
        except Exception:
            continue
    return found


def refresh_token_oauth(refresh_token):
    """用 refresh_token 换新 access_token，成功返回新 token 字符串，失败返回 None。"""
    try:
        params = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": refresh_token,
            "scope": REFRESH_SCOPE,
        })
        parsed = urlsplit(TOKEN_ENDPOINT)
        ctx = ssl.create_default_context()
        conn = http.client.HTTPSConnection(parsed.hostname, 443, context=ctx, timeout=30)
        conn.request("POST", parsed.path, body=params, headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": f"grok-pager/{GROK_VERSION}",
        })
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", "replace")
        conn.close()
        if resp.status == 200:
            return json.loads(body).get("access_token")
        return None
    except Exception:
        return None


def _token_is_expired(token, now=None):
    exp = _jwt_exp(token)
    if exp is None:
        return False
    return (now or time.time()) >= exp


def _jwt_exp(token):
    """解析 JWT exp（UTC 秒）。解析失败返回 None（不拦截）。"""
    try:
        part = token.split('.')[1]
        part += '=' * (-len(part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part))
        return payload.get('exp')
    except Exception:
        return None


SESSION_TTL = 7200.0        # 会话映射 2h 不活跃清理
BINDING_PENALTY = 25000.0   # 多会话分配时，已被绑定的 token 惩罚分（鼓励分散）
SESSION_MAP_FILE = None     # main 设置，默认 <token_dir 父目录>/session_map.json
_session_map = {}           # session_key -> label（会话粘性绑定表）
_session_ts = {}            # session_key -> last_seen
_session_last_save = 0.0


def _binding_count(label):
    try:
        return sum(1 for v in _session_map.values() if v == label)
    except Exception:
        return 0


def _sessions_cleanup(now):
    stale = [k for k, ts in _session_ts.items() if now - ts > SESSION_TTL]
    for k in stale:
        _session_map.pop(k, None)
        _session_ts.pop(k, None)
    if stale:
        print(f"[gateway] session map cleanup: removed {len(stale)} stale", flush=True)


def _sessions_save(force=False):
    global _session_last_save
    if not SESSION_MAP_FILE:
        return
    if not force and time.time() - _session_last_save < QUOTA_SAVE_INTERVAL:
        return
    _session_last_save = time.time()
    try:
        tmp = SESSION_MAP_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_session_map, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SESSION_MAP_FILE)
    except Exception:
        pass


def _sessions_load():
    if SESSION_MAP_FILE and os.path.exists(SESSION_MAP_FILE):
        try:
            with open(SESSION_MAP_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                now = time.time()
                for k, v in data.items():
                    _session_map[str(k)] = str(v)
                    _session_ts[str(k)] = now
                print(f"[gateway] session map loaded: {len(data)} entries", flush=True)
        except Exception as exc:
            print(f"[gateway] session map load failed: {exc!r}", flush=True)


def pick_token(session_key=None):
    """Sticky + quota-aware selection:
    1) sticky 优先：上次用的 token 若仍健康（未冷却/未过期/额度>0）直接复用（缓存连续性最大化）
    2) 否则选剩余额度最高且健康的 token 作为新 sticky
    """
    global _pool_next, _sticky_idx
    now = time.time()
    with _pool_lock:
        n = len(_token_pool)
        if n == 0:
            return None
        bound_label = None
        if session_key:
            bound_label = _session_map.get(session_key)
            if bound_label:
                for i in range(n):
                    t, label, ban_until, rt = _token_pool[i]
                    if label != bound_label:
                        continue
                    if now >= ban_until and not _token_is_expired(t, now) and _quota_get(label) > QUOTA_EXHAUSTED_THRESHOLD:
                        _session_ts[session_key] = now
                        return (t, label)
                print(f"[gateway] session {session_key[:21]} migrating from {bound_label} (unhealthy/exhausted)", flush=True)
        if 0 <= _sticky_idx < n:
            idx = _sticky_idx
            t, label, ban_until, rt = _token_pool[idx]
            if now >= ban_until and not _token_is_expired(t, now)                     and _quota_get(label) > QUOTA_EXHAUSTED_THRESHOLD:
                return (t, label)
        best_idx = -1
        best_score = -1e18
        self_heal_done_this_pass = False
        self_heal_candidate = None
        for i in range(n):
            t, label, ban_until, rt = _token_pool[i]
            if now < ban_until:
                continue
            if _token_is_expired(t, now):
                if not rt:
                    continue
                _pool_lock.release()
                new_t = refresh_token_oauth(rt)
                _pool_lock.acquire()
                if new_t:
                    _token_pool[i] = (new_t, label, 0.0, rt)
                    _persist_token(label, new_t)
                    stats_record(label, "refresh")
                    print(f"[gateway] token {label} EXPIRED, refreshed OK", flush=True)
                    t = new_t
                else:
                    stats_record(label, "refresh_fail")
                    print(f"[gateway] token {label} refresh FAILED, skipping", flush=True)
                    continue
            entry_q = _quota_est.get(label) or {}
            last_probe = entry_q.get("last_probe", 0) if isinstance(entry_q, dict) else 0
            if _quota_get(label) <= QUOTA_EXHAUSTED_THRESHOLD:
                # 额度耗尽候选：不参与正常竞争，只作为"全池无可用"时的兜底（30min 节流）
                if not self_heal_done_this_pass and now - last_probe >= PROBE_MIN_INTERVAL:
                    self_heal_candidate = i
                continue
            score = _quota_get(label) + (_pool_next * 1e-6)
            if session_key:
                score -= BINDING_PENALTY * _binding_count(label)
            if score > best_score:
                best_score = score
                best_idx = i
        if best_idx < 0:
            # 全池无可用 token：兜底重置一个过期探测 token（每请求最多 1 个，30min/个）
            if self_heal_candidate is not None:
                i = self_heal_candidate
                label = _token_pool[i][1]
                _quota_update(label, QUOTA_LIMIT)
                if isinstance(_quota_est.get(label), dict):
                    _quota_est[label]["last_probe"] = time.time()
                else:
                    _quota_est[label] = {"remaining": QUOTA_LIMIT, "last_probe": time.time()}
                _quota_save()
                print(f"[gateway] token {label} quota=0 but cooldown over, retry once (fallback)", flush=True)
                _sticky_idx = i
                return (_token_pool[i][0], label)
            return None
        if session_key:
            new_label = _token_pool[best_idx][1]
            _session_map[session_key] = new_label
            _session_ts[session_key] = now
            _sessions_cleanup(now)
            _sessions_save()
            print(f"[gateway] session {session_key[:21]} bound -> {new_label} (bindings={_binding_count(new_label)})", flush=True)
        else:
            _sticky_idx = best_idx
        _pool_next = (_pool_next + 1) % n
        return (_token_pool[best_idx][0], _token_pool[best_idx][1])


def _quota_get(label):
    entry = _quota_est.get(label)
    if isinstance(entry, dict):
        return entry.get("remaining", QUOTA_LIMIT)
    return QUOTA_LIMIT


def _quota_save(force=False):
    try:
        if not QUOTA_STATE_FILE:
            return
        now = time.time()
        if not force and _quota_last_save and now - _quota_last_save < QUOTA_SAVE_INTERVAL:
            return
        _quota_last_save = now
        tmp = QUOTA_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_quota_est, f, ensure_ascii=False, indent=2)
        os.replace(tmp, QUOTA_STATE_FILE)
    except Exception:
        pass


def _quota_load():
    global QUOTA_STATE_FILE
    try:
        if QUOTA_STATE_FILE and os.path.exists(QUOTA_STATE_FILE):
            with open(QUOTA_STATE_FILE, encoding="utf-8") as f:
                data = json.load(f)
            for label, v in (data or {}).items():
                if isinstance(v, dict):
                    _quota_est[label] = v
                elif isinstance(v, (int, float)):
                    _quota_est[label] = {"remaining": v, "actual": None, "limit": QUOTA_LIMIT, "updated_at": None}
    except Exception:
        pass


def _quota_update(label, used_tokens):
    entry = _quota_est.get(label)
    if not isinstance(entry, dict):
        entry = _quota_est[label] = {"remaining": QUOTA_LIMIT, "actual": None, "limit": QUOTA_LIMIT, "updated_at": None}
    entry["remaining"] = max(0, entry.get("remaining", QUOTA_LIMIT) - used_tokens)
    entry["updated_at"] = time.time()
    _quota_save()


def _quota_calibrate(label, actual, limit):
    entry = _quota_est.setdefault(label, {})
    entry["actual"] = actual
    entry["limit"] = limit
    entry["remaining"] = max(0, limit - actual)
    entry["updated_at"] = time.time()
    _quota_save()


def _quota_consume_from_usage(label, json_body):
    """200 响应 usage 学习：cached_tokens 不计消耗（缓存几乎免费）。"""
    try:
        usage = json_body.get("usage") or {}
        prompt = usage.get("prompt_tokens", 0)
        output = usage.get("output_tokens", 0)
        details = usage.get("prompt_tokens_details") or {}
        cached = details.get("cached_tokens", 0)
        charged = max(0, prompt - cached) + output
        if charged > 0:
            _quota_update(label, charged)
        return True
    except Exception:
        return False


def ban_token(token, seconds):
    global _sticky_idx
    with _pool_lock:
        for i, (t, label, _, rt) in enumerate(_token_pool):
            if t == token:
                _token_pool[i] = (t, label, time.time() + seconds, rt)
                if i == _sticky_idx:
                    _sticky_idx = -1
                print(f"[gateway] token {label} cooled down for {seconds}s", flush=True)
                return


def _persist_token(label, new_token):
    """把 refresh 后的新 access_token 写回原 JSON 文件（仅当唯一匹配）。"""
    try:
        entries = [e for e in _token_pool if e[1] == label]
        if len(entries) != 1:
            return
        rt = entries[0][3]
        for name in os.listdir(Handler.token_dir):
            p = os.path.join(Handler.token_dir, name)
            if not name.endswith(".json"):
                continue
            try:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("email") == label and data.get("refresh_token") == rt:
                    data["access_token"] = new_token
                    data["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    with open(p, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                    print(f"[gateway] persisted refreshed token to {name}", flush=True)
                    return
            except Exception:
                continue
    except Exception:
        pass


# ------------- 后台额度探测线程 -------------
PROBE_SCAN_INTERVAL = 15 * 60   # 每 15 分钟扫描一轮候选
_probe_last = {}                # label -> 上次探测时间


def _probe_one(token, label):
    """发极小请求探测额度。返回 'revived'/'exhausted'/'rate_limited'/'revoked'/'error'。"""
    try:
        parsed = urlsplit(Handler.upstream)
        conn = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=30)
        body = json.dumps({"model": "grok-4.6", "messages": [{"role": "user", "content": "ok"}], "max_tokens": 1}).encode()
        hdrs = {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + token,
            "x-grok-client-version": GROK_VERSION,
            "x-grok-client-identifier": "grok-pager",
        }
        conn.request("POST", parsed.path + "/chat/completions", body=body, headers=hdrs)
        resp = conn.getresponse()
        status = resp.status
        rbody = resp.read(65536).decode("utf-8", "replace")
        conn.close()
        if status == 200:
            return "revived"
        if status in (429, 402):
            if "free-usage-exhausted" in rbody:
                m = re.search(r"\(actual/limit\):\s*(\d+)\s*/\s*(\d+)", rbody)
                if m:
                    _quota_calibrate(label, int(m.group(1)), int(m.group(2)))
                return "exhausted"
            return "rate_limited"
        if status in (401, 403):
            return "revoked"
        return "error"
    except Exception:
        return "error"


def _reset_quota_full(label):
    entry = _quota_est.setdefault(label, {})
    entry["remaining"] = QUOTA_LIMIT
    entry["actual"] = None
    entry["updated_at"] = time.time()
    _quota_save(force=True)


def _background_probe_loop():
    """独立线程：每 15 分钟找「冷却已结束 + 额度=0 + 距上次探测超阈值」的 token 逐个探测额度恢复。"""
    while True:
        time.sleep(PROBE_SCAN_INTERVAL)
        now = time.time()
        candidates = []
        with _pool_lock:
            for (t, label, ban_until, rt) in list(_token_pool):
                if now < ban_until:
                    continue
                if _quota_get(label) > QUOTA_EXHAUSTED_THRESHOLD:
                    continue
                if now - _probe_last.get(label, 0) < PROBE_MIN_INTERVAL:
                    continue
                if not rt:
                    continue
                candidates.append((t, label, ban_until, rt))
        for (t, label, ban_until, rt) in candidates:
            _probe_last[label] = time.time()
            probe_token = t
            if _token_is_expired(t):
                new_t = refresh_token_oauth(rt)
                if not new_t:
                    print(f"[gateway] probe: {label} access expired + refresh FAILED, skip", flush=True)
                    continue
                probe_token = new_t
                with _pool_lock:
                    for i, (tt, ll, bb, rr) in enumerate(_token_pool):
                        if tt == t:
                            _token_pool[i] = (new_t, ll, bb, rr)
                            break
                _persist_token(label, new_t)
            result = _probe_one(probe_token, label)
            print(f"[gateway] probe: {label} -> {result}", flush=True)
            if result == "revived":
                _reset_quota_full(label)
                print(f"[gateway] probe: {label} quota restored to {QUOTA_LIMIT}", flush=True)
            elif result == "revoked":
                with _pool_lock:
                    _token_pool[:] = [(tt, ll, bb, rr) for tt, ll, bb, rr in _token_pool if ll != label]
                print(f"[gateway] probe: {label} revoked, removed from pool", flush=True)
            # exhausted / rate_limited / error：保持 0，顺延下次探测


def probe_all_tokens():
    """一次性探测全部 token，恢复实际/剩余额度。返回汇总结果。"""
    results = {"revived": [], "exhausted": [], "revoked": [], "rate_limited": [], "error": []}
    with _pool_lock:
        snapshot = list(_token_pool)
    for (t, label, ban_until, rt) in snapshot:
        prior_remaining = _quota_get(label)
        probe_token = t
        if _token_is_expired(t):
            if not rt:
                results["error"].append(label)
                continue
            new_t = refresh_token_oauth(rt)
            if not new_t:
                print(f"[probe-all] {label} access expired + refresh FAILED", flush=True)
                results["error"].append(label)
                continue
            probe_token = new_t
            with _pool_lock:
                for i, (tt, ll, bb, rr) in enumerate(_token_pool):
                    if tt == t:
                        _token_pool[i] = (new_t, ll, bb, rr)
                        break
            _persist_token(label, new_t)
        result = _probe_one(probe_token, label)
        _probe_last[label] = time.time()
        if result == "revived":
            if prior_remaining <= QUOTA_EXHAUSTED_THRESHOLD:
                _reset_quota_full(label)
            results["revived"].append(label)
        elif result == "revoked":
            with _pool_lock:
                _token_pool[:] = [(tt, ll, bb, rr) for tt, ll, bb, rr in _token_pool if ll != label]
            results["revoked"].append(label)
        else:
            results.setdefault(result, []).append(label)
        print(f"[probe-all] {label} -> {result}", flush=True)
    _quota_save(force=True)
    return results


def _split_sse(raw):
    """把 SSE 字节流切分为 [(event_name, data_dict_or_None, raw_block), ...]。"""
    blocks = raw.split(b"\n\n")
    out = []
    for b in blocks:
        b = b.strip(b"\r\n")
        if not b:
            continue
        lines = b.split(b"\n")
        ev = None
        data = None
        for ln in lines:
            if ln.startswith(b"event: "):
                ev = ln[7:].decode("utf-8", "replace").strip()
            elif ln.startswith(b"data: "):
                dd = ln[6:].strip()
                try:
                    data = json.loads(dd.decode("utf-8", "replace"))
                except Exception:
                    data = None
        out.append((ev, data, b + b"\n\n"))
    return out


def _emit_sse_event(name, data, seq, evid):
    lines = [f"event: {name}", f"data: {json.dumps(data, ensure_ascii=False)}"]
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def _filter_empty_edit_events(events):
    """检测 responses SSE 流中的 Edit 工具调用 old_string==new_string，替换为文本输出。
    返回 (new_events, replaced_with_text_or_None)。"""
    # 找出需要删除的事件区间与消息输出
    idx = 0
    n = len(events)
    text = None
    while idx < n:
        name, data, _ = events[idx]
        item = None
        if name == "response.output_item.added" and isinstance(data, dict):
            item = data.get("item") or {}
        if isinstance(item, dict) and item.get("type") == "function_call" and item.get("name") == "Edit":
            # 收集这个工具调用的所有 fragment
            args_frag = []
            end_idx = idx
            item_id = item.get("id", "")
            out_idx = data.get("output_index", 0)
            j = idx + 1
            done_args = None
            while j < n:
                ename, edata, _ = events[j]
                if ename == "response.function_call_arguments.delta" and isinstance(edata, dict):
                    args_frag.append((edata.get("delta") or ""))
                    j += 1
                    end_idx = j
                    continue
                if ename == "response.function_call_arguments.done" and isinstance(edata, dict):
                    done_args = edata.get("arguments")
                    end_idx = j
                    j += 1
                    continue
                if ename == "response.output_item.done" and isinstance(edata, dict):
                    dit = edata.get("item") or {}
                    if dit.get("arguments"):
                        done_args = dit.get("arguments")
                    end_idx = j
                    break
                end_idx = j
                j += 1
            if done_args:
                exp = done_args.strip()
            else:
                exp = "".join(args_frag).strip()
            old = new = None
            try:
                obj = json.loads(exp)
                old = obj.get("old_string")
                new = obj.get("new_string")
            except Exception:
                pass
            if old is not None and new is not None and old == new:
                # 替代方案：把空 Edit 改写成无害 Bash 工具调用（echo），
                # 让 Claude Code 拿到 tool_result 后自动继续后续任务，而不是以文本结束回合。
                log_skip = (f"empty-edit skipped: {str(obj.get('file_path', ''))[:120]}")
                text = log_skip
                new_args_obj = {
                    "command": (f"echo '[gateway] empty edit skipped: old_string == new_string "
                                f"for file: {str(obj.get('file_path', ''))[:120]}; no change needed.'"),
                    "description": "no-op echo for skipped empty edit",
                }
                new_args = json.dumps(new_args_obj)
                fn_id = f"fn_skipped_{item_id[-8:]}" if item_id else "fn_skipped"
                # 工具名：用请求里真正的 Bash 工具名，找不到就用 execute_bash
                bash_name = _REQ_TOOLS.get("bash_name") or "execute_bash"
                repl = [
                    _emit_sse_event("response.output_item.added", {
                        "type": "response.output_item.added", "sequence_number": idx + 1,
                        "output_index": out_idx,
                        "item": {"id": fn_id, "type": "function_call", "status": "in_progress",
                                 "name": bash_name, "arguments": "", "call_id": fn_id},
                    }, idx + 1, None),
                    _emit_sse_event("response.function_call_arguments.delta", {
                        "type": "response.function_call_arguments.delta", "sequence_number": idx + 2,
                        "item_id": fn_id, "output_index": out_idx, "delta": new_args,
                    }, idx + 2, None),
                    _emit_sse_event("response.function_call_arguments.done", {
                        "type": "response.function_call_arguments.done", "sequence_number": idx + 3,
                        "item_id": fn_id, "output_index": out_idx, "arguments": new_args,
                    }, idx + 3, None),
                    _emit_sse_event("response.output_item.done", {
                        "type": "response.output_item.done", "sequence_number": idx + 4,
                        "item_id": fn_id, "output_index": out_idx,
                        "item": {"id": fn_id, "type": "function_call", "role": "assistant",
                                 "status": "completed", "name": bash_name,
                                 "arguments": new_args, "call_id": fn_id},
                    }, idx + 4, None),
                ]
                new_events = events[:idx] + [(None, None, rb) for rb in repl] + events[end_idx + 1:]
                return new_events, text
            idx = end_idx + 1
            continue
        idx += 1
    return events, None


def _filter_empty_edit_raw(raw_response):
    """对整段 upstream responses SSE 流执行空 Edit 过滤，返回替换后的字节流。"""
    global _empty_edit_streak
    try:
        if b"response.output_item.added" not in raw_response:
            if _empty_edit_streak > 0:
                _empty_edit_streak -= 1
            return raw_response
        events = _split_sse(raw_response)
        events, text = _filter_empty_edit_events(events)
        if text is None:
            if _empty_edit_streak > 0:
                _empty_edit_streak -= 1
            return raw_response
        _empty_edit_streak += 1
        print(f"[gateway] EMPTY EDIT detected & replaced with no-op bash ({len(text)} chars) "
              f"(streak={_empty_edit_streak})", flush=True)
        if _empty_edit_streak >= EMPTY_EDIT_LOOP_LIMIT:
            print(f"[gateway] empty edit streak >= {EMPTY_EDIT_LOOP_LIMIT}, "
                  f"disabling FORCED tool_choice to break the loop", flush=True)
        stats_record("", "empty_edit")
        seen_completed = False
        out = []
        for _, _, rb in events:
            if rb and b"response.completed" in rb:
                if seen_completed:
                    continue
                seen_completed = True
            if rb:
                out.append(rb)
        return b"".join(out)
    except Exception as e:
        print(f"[gateway] empty-edit filter error: {e!r}", flush=True)
        return raw_response


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True   # 客户端侧 TCP_NODELAY：流式块即时推送
    wbufsize = 65536                 # 写缓冲，减少 syscall
    upstream = DEFAULT_UPSTREAM
    ban_seconds = 300
    token_dir = ""
    force_tool_choice = False
    force_non_stream = False
    filter_empty_edit = False
    shell_hint = False
    repeat_guard = False

    def log_message(self, fmt, *args):
        print(f"[gateway] {self.command} {self.path} <- {self.headers.get('User-Agent','')[:60]}", flush=True)

    def _proxy(self, method):
        path = self.path
        print(f"[gateway] req {method} {path} te={self.headers.get('Transfer-Encoding','-')} cl={self.headers.get('Content-Length','-')}", flush=True)
        if _VERBOSE:
            for hk, hv in self.headers.items():
                print(f"[gateway]   H {hk}: {hv[:80]}", flush=True)
        if path.startswith("/v1/"):
            path = path[len("/v1"):]
        url = Handler.upstream + path
        parsed = urlsplit(url)
        assert parsed.scheme == "https", "upstream must be https"
        conn = _acquire_conn(parsed.hostname)
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
        if body is not None and ("/responses" in self.path or "/chat/completions" in self.path):
            try:
                bj = json.loads(body)
                print(f"[gateway] fwd-model={bj.get('model')} stream={bj.get('stream')} keys={list(bj.keys())[:12]} tools={len(bj.get('tools') or [])}", flush=True)
                tools_list = bj.get("tools") or []
                latest_tools = []
                for tl_ in tools_list:
                    tname = tl_.get("name") if isinstance(tl_, dict) else None
                    if not tname and isinstance(tl_, dict) and isinstance(tl_.get("function"), dict):
                        tname = tl_["function"].get("name")
                    if tname:
                        latest_tools.append(tname)
                # 优先 Bash 类工具（Claude Code 用 execute_bash / ExecuteBash / Bash / bash）
                bs_name = None
                for cand in ("execute_bash", "ExecuteBash", "Bash", "bash"):
                    if cand in latest_tools:
                        bs_name = cand
                        break
                _REQ_TOOLS["names"] = latest_tools
                _REQ_TOOLS["bash_name"] = bs_name
                if Handler.force_non_stream and bj.get("stream") and "/responses" in self.path:
                    bj["stream"] = False
                    body = json.dumps(bj).encode()
                    print("[gateway] FORCED non-stream", flush=True)
                if tools_list and Handler.force_tool_choice and _empty_edit_streak < EMPTY_EDIT_LOOP_LIMIT:
                    has_tool_history = False
                    conversation = bj.get("messages")
                    if conversation is None:
                        conversation = bj.get("input")
                    last_user_text = ""
                    if isinstance(conversation, list):
                        for msg in conversation[:-1]:
                            if not isinstance(msg, dict):
                                continue
                            if msg.get("tool_calls") or msg.get("tool_call_id"):
                                has_tool_history = True
                        for msg in reversed(conversation):
                            if isinstance(msg, dict) and msg.get("role") == "user":
                                c = msg.get("content")
                                if isinstance(c, list):
                                    c = " ".join(
                                        p.get("text", "") if isinstance(p, dict) else str(p)
                                        for p in c
                                    )
                                last_user_text = str(c or "")
                                break
                    clean_user_text = strip_client_wrappers(last_user_text)
                    _u = (clean_user_text or "").strip().lower()
                    # 剥离一切 XML 风格标签（如 <dcp-message-id>m123</dcp-message-id>、<env> 等）
                    _u = re.sub(r"<[^<>]{1,120}>", " ", _u)
                    # 再剥非字母/汉字字符（零宽字符、控制符等不可见注入）
                    _u = "".join(
                        ch for ch in _u
                        if (ch.isascii() and ch.isalpha()) or ("\u4e00" <= ch <= "\u9fff")
                    )
                    is_greeting = _u in {
                        "hi", "hi there", "hello", "hey", "yo", "hey there",
                        "你好", "您好", "嗨", "哈喽", "哈啰", "在吗", "在么",
                        "再见", "拜拜", "bye", "good night", "good morning", "晚安", "早安",
                    }
                    is_ack = _u in {
                        "ok", "okay", "好的", "好", "明白", "了解", "收到", "嗯", "哦", "哦哦",
                        "行", "可以的", "辛苦了", "谢谢", "多谢", "感谢", "thanks", "thank you", "thx", "ty",
                    }
                    has_assistant_text = False
                    if isinstance(conversation, list):
                        for msg in conversation[:-1]:
                            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                                continue
                            c = msg.get("content")
                            if isinstance(c, list):
                                c = " ".join(
                                    p.get("text", "") if isinstance(p, dict) else str(p)
                                    for p in c
                                )
                            if isinstance(c, str) and len(c.strip()) >= 80:
                                has_assistant_text = True
                                break
                    small_talk = bool(
                        clean_user_text
                        and (is_greeting or (not has_tool_history and is_ack))
                    )
                    if small_talk:
                        if bj.get("tool_choice") != "auto":
                            old_tc = bj.get("tool_choice")
                            bj["tool_choice"] = "auto"
                            body = json.dumps(bj).encode()
                            print(f"[gateway] relaxed tool_choice: {json.dumps(old_tc)[:40] if old_tc is not None else 'none'} -> auto ({'greeting' if is_greeting else 'small talk'})", flush=True)
                    elif has_assistant_text or _DEGEN_STREAK["n"] >= DEGEN_FORCED_LIMIT:
                        if _DEGEN_STREAK["n"] >= DEGEN_FORCED_LIMIT and not has_assistant_text:
                            print("[gateway] degenerate-loop guard (%d forced fast-empty responses): keep client tool_choice" % _DEGEN_STREAK["n"], flush=True)
                        else:
                            print("[gateway] keep client tool_choice (assistant already producing text)", flush=True)
                    else:
                        tc = bj.get("tool_choice")
                        cur = tc.get("type") if isinstance(tc, dict) else str(tc)
                        if cur != "required":
                            old = json.dumps(tc)[:80] if tc is not None else "none"
                            bj["tool_choice"] = "required"
                            body = json.dumps(bj).encode()
                            forced_now = True
                            print(f"[gateway] FORCED tool_choice: {old} -> required", flush=True)
                    # shell-hint：检测 bash 类工具时注入 PowerShell 环境提示（幂等，只注一次）
                    if Handler.shell_hint:
                        try:
                            tools_list_ = bj.get("tools") or []
                            has_bash_tool = any(
                                (isinstance(t, dict) and (
                                    str((t.get("function") or {}).get("name", "")).lower() in ("bash", "execute_bash", "run_command", "shell")
                                    or str(t.get("name", "")).lower() in ("bash", "execute_bash", "run_command", "shell")
                                ))
                                for t in tools_list_ if isinstance(t, dict)
                            )
                            msgs_ = bj.get("messages")
                            if has_bash_tool and isinstance(msgs_, list) and msgs_:
                                marker = "[gateway] Shell environment:"
                                first_txt = ""
                                m0 = msgs_[0]
                                c0 = m0.get("content") if isinstance(m0, dict) else None
                                if isinstance(c0, str):
                                    first_txt = c0
                                elif isinstance(c0, list):
                                    first_txt = " ".join(p.get("text", "") for p in c0 if isinstance(p, dict))
                                if marker not in first_txt and marker not in json.dumps(bj)[:4000]:
                                    hint = (
                                        "[gateway] Shell environment: Windows PowerShell. "
                                        "Generate PowerShell syntax ONLY: use Get-ChildItem instead of ls/dir; "
                                        "use Select-Object -First N instead of head/tail; "
                                        "do NOT use &&, /b, /ad, /s flags; quote paths normally. "
                                        "If a command fails with a PowerShell error, rewrite it in pure PowerShell syntax. "
                                        "[gateway] Grounding rule: NEVER claim a file or directory exists, and never "
                                        "describe its contents or line count, unless a tool result in THIS conversation "
                                        "actually listed or read it. When asked to confirm existence, ALWAYS verify with "
                                        "a listing/read tool first and cite the real result; if not found, say so plainly. "
                                        "Conciseness rule: end your reply IMMEDIATELY once the task is complete - do NOT "
                                        "append repeated status lines, farewells, or variants like 'Ready', 'Done', "
                                        "'已完成', '(随时可继续)', 'Status: ...' after the answer."
                                    )
                                    if isinstance(m0, dict) and m0.get("role") == "system" and isinstance(m0.get("content"), str):
                                        m0["content"] = m0["content"].rstrip() + "\n\n" + hint
                                    else:
                                        msgs_.insert(0, {"role": "system", "content": hint})
                                    body = json.dumps(bj).encode()
                                    print("[gateway] SHELL HINT injected (PowerShell)", flush=True)
                        except Exception as e:
                            print(f"[gateway] shell-hint skipped: {e!r}", flush=True)
                hdrs["Content-Length"] = str(len(body))
                try:
                    _n_tags = _sanitize_conversation(bj)
                    if _n_tags:
                        body = json.dumps(bj).encode()
                        hdrs["Content-Length"] = str(len(body))
                        print(f"[gateway] DCP stripped {_n_tags} meta tag(s)", flush=True)
                except Exception:
                    pass
            except Exception as e:
                print(f"[gateway] body not json: {e!r}", flush=True)

        forced_now = False
        used_tokens = set()
        status = None
        retry_after = None
        max_cooldown = 0
        exhausted_all = False
        session_key = None
        try:
            sid_hdr = self.headers.get("x-claude-code-session-id") or self.headers.get("x-session-id")
            if sid_hdr:
                session_key = "sid:" + sid_hdr.strip()[:64]
            elif body:
                try:
                    sk_obj = json.loads(body)
                except Exception:
                    sk_obj = None
                if isinstance(sk_obj, dict):
                    msgs = sk_obj.get("messages") or sk_obj.get("input") or []
                    sys_txt = ""
                    first_user = ""
                    if isinstance(msgs, list):
                        for m in msgs:
                            if not isinstance(m, dict):
                                continue
                            c = m.get("content")
                            if isinstance(c, list):
                                c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
                            c = "" if c is None else str(c)
                            role = str(m.get("role", ""))
                            if role == "system" and not sys_txt:
                                sys_txt = c[:512]
                            if role == "user" and not first_user:
                                first_user = c[:512]
                    raw = (sys_txt + "\x00" + first_user).encode("utf-8", "ignore")
                    if len(raw) >= 8:
                        session_key = "hash:" + hashlib.sha256(raw).hexdigest()[:16]
        except Exception:
            session_key = None
        while True:
            picked = pick_token(session_key)
            if picked is None:
                if exhausted_all or max_cooldown > 0:
                    self._reply(429, {
                        "code": "subscription:free-usage-exhausted",
                        "error": {
                            "message": "所有 token 免费额度已耗尽（free-usage-exhausted），已冷却，Retry-After 后重试",
                            "retry_after": max_cooldown,
                        },
                    })
                elif retry_after:
                    self._reply_429(retry_after)
                else:
                    self._reply(429, {"error": {"message": "all tokens throttled or expired"}})
                _release_conn(conn)
                return
            token, label = picked
            if token in used_tokens:
                continue
            used_tokens.add(token)
            hdrs_ = dict(hdrs)
            hdrs_["Authorization"] = "Bearer " + token
            try:
                conn, resp = _send_upstream(conn, parsed.hostname, method, parsed.path, body, hdrs_)
                status = resp.status
                if status in (429,):
                    body_429 = b""
                    try:
                        while len(body_429) < 65536:
                            body_chunk = resp.read(65536)
                            if not body_chunk:
                                break
                            body_429 += body_chunk
                    except Exception:
                        pass
                    is_exhausted = b"free-usage-exhausted" in body_429 or b"free_usage_exhausted" in body_429
                    if is_exhausted:
                        ban_secs = Handler.exhaust_ban_hours * 3600
                        print(f"[gateway] token {label} 免费额度耗尽 (free-usage-exhausted)，冷却 {Handler.exhaust_ban_hours}h 并切换下一 token", flush=True)
                        stats_record(label, "exhausted")
                        _quota_update(label, QUOTA_LIMIT * 2)   # 打到 0：估算额度清零
                        try:
                            text = body_429.decode("utf-8", "replace")
                            actual = limit = None
                            try:
                                err = json.loads(text) or {}
                                usage = ((err.get("error") or {}).get("usage") or {}) if isinstance((err.get("error") or {}), dict) else {}
                                actual, limit = usage.get("actual"), usage.get("limit")
                            except Exception:
                                pass
                            if not isinstance(actual, (int, float)) or not isinstance(limit, (int, float)):
                                m = re.search(r"\(actual/limit\):\s*(\d+)\s*/\s*(\d+)", text)
                                if m:
                                    actual, limit = int(m.group(1)), int(m.group(2))
                            if isinstance(actual, (int, float)) and isinstance(limit, (int, float)):
                                _quota_calibrate(label, actual, limit)
                                print(f"[gateway] quota calibrated: {label} actual={actual} limit={limit}", flush=True)
                        except Exception:
                            pass
                        ban_token(token, ban_secs)
                        max_cooldown = max(max_cooldown, ban_secs)
                        exhausted_all = True
                        # 终结 retry：全池都耗尽时继续试其他 token 只会连环撞墙，直接快速返回 429
                        self._reply(429, {
                            "code": "subscription:free-usage-exhausted",
                            "error": {
                                "message": "所有 token 免费额度已耗尽（free-usage-exhausted），已冷却，Retry-After 后重试",
                                "retry_after": max_cooldown,
                            },
                        })
                        _release_conn(conn)
                        return
                    else:
                        ra = resp.getheader("Retry-After")
                        try:
                            ra = int(ra) if ra else None
                        except Exception:
                            ra = None
                        if ra and (retry_after is None or ra > retry_after):
                            retry_after = ra
                        ban_token(token, min(Handler.ban_seconds, ra or Handler.ban_seconds))
                        stats_record(label, "429")
                    continue
                if status in (401, 403):
                    exp = _jwt_exp(token)
                    rt_entry = None
                    with _pool_lock:
                        for i, (t, _l, _b, rtk) in enumerate(_token_pool):
                            if t == token:
                                rt_entry = (i, rtk)
                                break
                    if rt_entry and rt_entry[1] and (exp is None or exp < time.time()):
                        print(f"[gateway] token {label} 401 but refresh_token present, refreshing", flush=True)
                        new_t = refresh_token_oauth(rt_entry[1])
                        if new_t:
                            with _pool_lock:
                                _token_pool[rt_entry[0]] = (new_t, label, 0.0, rt_entry[1])
                                _persist_token(label, new_t)
                                stats_record(label, "refresh")
                                resp.read()
                                continue
                        print(f"[gateway] token {label} refresh during 401 FAILED, removing", flush=True)
                        with _pool_lock:
                            _token_pool[:] = [(t, lb, bt, rt2) for t, lb, bt, rt2 in _token_pool if t != token]
                    elif exp is not None:
                        print(f"[gateway] token {label} rejected (exp={exp}), removing from pool", flush=True)
                        with _pool_lock:
                            _token_pool[:] = [(t, lb, bt, rt2) for t, lb, bt, rt2 in _token_pool if t != token]
                    else:
                        with _pool_lock:
                            _token_pool[:] = [(t, lb, bt, rt2) for t, lb, bt, rt2 in _token_pool if t != token]
                        print(f"[gateway] token {label} 401 without refresh_token, removing from pool", flush=True)
                    stats_record(label, "401")
                    resp.read()
                    continue
                t_start = time.time()
                print(f"[gateway] -> upstream status {status} for {self.command} {self.path} token {label}", flush=True)
                stats_record(label, "ok")
                resp_body = b""
                upstream_ce = ""
                for k, v in resp.getheaders():
                    if k.lower() == "content-encoding":
                        upstream_ce = v
                try:
                    self.send_response(status)
                    del_h = ("content-length", "connection", "transfer-encoding")
                    for k, v in resp.getheaders():
                        if k.lower() not in del_h and k.lower() != "content-encoding":
                            self.send_header(k, v)
                    self.send_header("Transfer-Encoding", "chunked")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                except Exception as e:
                    print(f"[gateway] response header write failed: {e!r}", flush=True)
                    conn.close()
                    return
                stream_gz = None
                if upstream_ce and upstream_ce.lower() == "gzip":
                    import gzip as _gz
                    stream_gz = _gz.GzipFile(fileobj=resp)
                acc = _DUMP_BODY   # SSE 流不累积全量体（省内存拷贝）；JSON 响应用于 usage 解析
                # Repetition Guard 状态：检测同一行文本重复输出（grok 退化形态④收尾复读）
                _rep_lines = {}
                _rep_tail = ""
                _is_responses_sse = "/responses" in self.path
                guard_hit = False

                def _rep_feed(text):
                    """累积文本并统计行出现次数；返回 True 表示某行重复超限。"""
                    nonlocal _rep_tail
                    if not text:
                        return False
                    _rep_tail += text
                    if len(_rep_tail) > 8192:
                        _rep_tail = _rep_tail[-4096:]
                    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                    for ln in lines:
                        if len(ln) >= 6:   # 忽略过短的行（标点/单符号）
                            _rep_lines[ln] = _rep_lines.get(ln, 0) + 1
                            if _rep_lines[ln] >= 4:
                                return True
                    return False

                def _rep_extract(chunk):
                    """从 SSE/JSON chunk 提取文本增量。"""
                    out = []
                    try:
                        s = chunk.decode("utf-8", "replace")
                        for line in s.splitlines():
                            if not line.startswith("data:"):
                                continue
                            payload = line[5:].strip()
                            if not payload or payload == "[DONE]":
                                continue
                            try:
                                ev = json.loads(payload)
                            except Exception:
                                continue
                            if isinstance(ev, dict):
                                if ev.get("type") == "response.output_text.delta":
                                    d = ev.get("delta")
                                    if isinstance(d, str):
                                        out.append(d)
                                elif "choices" in ev:
                                    for chd in ev["choices"]:
                                        dd = chd.get("delta") or {}
                                        cc = dd.get("content")
                                        if isinstance(cc, str):
                                            out.append(cc)
                    except Exception:
                        pass
                    return "".join(out)

                chunk = (stream_gz.read1(262144) if stream_gz else resp.read1(262144)) or b""
                while chunk:
                    if not acc and not resp_body and chunk[:1] == b"{":
                        acc = True
                    if acc:
                        resp_body += chunk
                    if b'[DONE]' in chunk:
                        print(f"[gateway] SSE [DONE] seen after {time.time()-t_start:.1f}s", flush=True)
                    if b'response.completed' in chunk:
                        print(f"[gateway] response.completed seen at {time.time()-t_start:.1f}s", flush=True)
                    if not guard_hit and Handler.repeat_guard:
                        try:
                            if _rep_feed(_rep_extract(chunk)):
                                guard_hit = True
                                print(f"[gateway] REPEAT GUARD triggered at {time.time()-t_start:.1f}s (repeated tail line)", flush=True)
                                stats_record(label, "repeat_guard")
                                break   # 停止透传；下方伪造正常收尾
                        except Exception:
                            pass
                    try:
                        self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                        self.wfile.flush()
                    except Exception as e:
                        print(f"[gateway] stream write aborted after {time.time()-t_start:.1f}s: {e!r}", flush=True)
                        break
                    chunk = (stream_gz.read1(262144) if stream_gz else resp.read1(262144)) or b""
                try:
                    # 排空剩余上游数据（写中断时防止污染连接池复用）
                    while (stream_gz.read1(262144) if stream_gz else resp.read1(262144)):
                        pass
                except Exception:
                    pass
                if guard_hit:
                    # 伪造正常收尾，让客户端认为流完整结束（避免重试/挂起）
                    try:
                        if _is_responses_sse:
                            tail_ev = (
                                b'event: response.completed\r\n'
                                b'data: {"type":"response.completed","sequence_number":999999,'
                                b'"response":{"id":"resp_guard_truncated","object":"response",'
                                b'"status":"completed","output":[],"usage":{"input_tokens":0,"output_tokens":0}}}\r\n\r\n'
                                b'data: [DONE]\r\n\r\n'
                            )
                        else:
                            tail_ev = (
                                b'data: {"id":"chatcmpl-guard","object":"chat.completion.chunk",'
                                b'"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\r\n\r\n'
                                b'data: [DONE]\r\n\r\n'
                            )
                        self.wfile.write(b"%x\r\n" % len(tail_ev) + tail_ev + b"\r\n")
                        self.wfile.flush()
                    except Exception:
                        pass
                try:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except Exception:
                    pass
                print(f"[gateway] upstream body EOF after {time.time()-t_start:.1f}s total, resp_rcvd={len(resp_body)}", flush=True)
                if forced_now:
                    _el = time.time() - t_start
                    if _el < 1.6:
                        _DEGEN_STREAK["n"] += 1
                    else:
                        _DEGEN_STREAK["n"] = 0
                try:
                    if resp_body[:1] == b"{":
                        try:
                            _quota_consume_from_usage(label, json.loads(resp_body.decode("utf-8", "replace")))
                        except Exception:
                            pass
                except Exception:
                    pass
                if _DUMP_BODY:
                    try:
                        with open(r'C:\Users\fr_li\AppData\Local\Temp\opencode\gw_last_body.bin', 'wb') as fb:
                            fb.write(resp_body)
                    except Exception:
                        pass
                _release_conn(conn)
                return
            except Exception as e:
                print(f"[gateway] ERROR {self.command} {self.path}: {e!r}", flush=True)
                try:
                    self._reply(502, {"error": {"message": f"gateway error: {e}"}})
                except Exception:
                    pass
                conn.close()
                return
        self._reply(429, {"error": {"message": "all tokens throttled or expired"}})
        conn.close()

    def _reply_429(self, retry_after):
        try:
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", str(retry_after))
            self.end_headers()
            self.wfile.write(json.dumps({"error": {"message": "rate limited, retry after", "retry_after": retry_after}}).encode())
        except Exception:
            pass

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


class ControlHandler(BaseHTTPRequestHandler):
    """控制端口：GET /status（池健康度快照）、POST /refresh（强制 refresh 全部过期 token）。"""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/status":
            self._json(200, snapshot_status())
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path == "/refresh":
            with _pool_lock:
                pairs = [(idx, t, label, rt) for idx, (t, label, _, rt) in enumerate(_token_pool)]
            refreshed = 0
            removed = 0
            for idx, t, label, rt in pairs:
                if not rt:
                    continue
                if not _token_is_expired(t):
                    continue
                new_t = refresh_token_oauth(rt)
                if new_t:
                    with _pool_lock:
                        for i2, (t2, lb2, _bt2, _rt2) in enumerate(_token_pool):
                            if lb2 == label:
                                _token_pool[i2] = (new_t, label, 0.0, rt)
                                break
                    _persist_token(label, new_t)
                    stats_record(label, "refresh")
                    refreshed += 1
                    print(f"[gateway] control refresh OK: {label}", flush=True)
                else:
                    with _pool_lock:
                        _token_pool[:] = [(t2, lb2, bt2, rt2) for t2, lb2, bt2, rt2 in _token_pool if t2 != t]
                    stats_record(label, "refresh_fail")
                    removed += 1
                    print(f"[gateway] control refresh FAILED (removed): {label}", flush=True)
            self._json(200, {"ok": True, "refreshed": refreshed, "removed": removed})
        elif self.path == "/probe":
            print("[gateway] control: manual probe-all triggered", flush=True)
            results = probe_all_tokens()
            summary = {k: len(v) for k, v in results.items()}
            self._json(200, {"ok": True, "summary": summary, **results})
        else:
            self._json(404, {"ok": False, "error": "not found"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", default=None)
    ap.add_argument("--token-file", default=None)
    ap.add_argument("--token-env", default=None)
    ap.add_argument("--token-dir", default=None, help="目录：扫描所有含 access_token 的 json / 文本 token 文件")
    ap.add_argument("--port", type=int, default=40200)
    ap.add_argument("--upstream", default=DEFAULT_UPSTREAM)
    ap.add_argument("--ban-seconds", type=int, default=300, help="429/401 后该 token 冷却秒数（默认 300）")
    ap.add_argument("--exhaust-ban-hours", type=float, default=24.0, help="免费额度耗尽(free-usage-exhausted)后冷却小时数（默认 24）")
    ap.add_argument("--force-tool-choice", action="store_true",
                    help="带 tools 的 responses/chat 请求强制 tool_choice=required（修复 grok free 不真调工具只输出文字）")
    ap.add_argument("--filter-empty-edit", action="store_true",
                    help="过滤响应中的 old_string==new_string 的空 Edit 工具调用，替换为提示文本（防 Claude Code 编辑死循环）")
    ap.add_argument("--force-non-stream", action="store_true",
                    help="把 responses 请求的 stream 强制改为 False（上游返回完整 JSON 而非 SSE，规避 cc-switch 流式解码失败）")
    ap.add_argument("--shell-hint", action="store_true",
                    help="inject Windows PowerShell hint when bash-like tools present")
    ap.add_argument("--repeat-guard", action="store_true",
                    help="truncate streaming responses when the same output line repeats 4+ times (grok degeneration)")
    ap.add_argument("--control-port", type=int, default=0,
                    help="控制端口（默认 0=关闭；设如 40201 可访问 /status、POST /refresh）")
    ap.add_argument("--quota-state-file", default=None,
                    help="额度状态持久化文件（默认 <token_dir父目录>/quota_state.json）")
    ap.add_argument("--session-map-file", default=None,
                    help="会话粘性映射持久化文件（默认 <token_dir父目录>/session_map.json）")
    args = ap.parse_args()

    entries = []
    if args.token_dir and os.path.isdir(args.token_dir):
        entries.extend(collect_tokens(args.token_dir))
        print(f"[gateway] loaded {len(entries)} token(s) from {args.token_dir}", flush=True)
    if not entries:
        src = args.token or (open(args.token_file, encoding="utf-8").read() if args.token_file else None)
        if args.token_env:
            src = os.environ.get(args.token_env)
        if not src:
            print("no token source given (use --token/--token-file/--token-dir)", file=sys.stderr)
            sys.exit(2)
        entries.append((load_token(src), "single", "", ""))

    now = time.time()
    with _pool_lock:
        _token_pool.clear()
        _token_pool.extend((t, lb, 0.0, rt) for t, lb, rt, _p in entries)

    Handler.upstream = args.upstream.rstrip("/")
    Handler.ban_seconds = args.ban_seconds
    Handler.token_dir = args.token_dir or ""
    global QUOTA_STATE_FILE
    if args.quota_state_file:
        QUOTA_STATE_FILE = args.quota_state_file
    elif args.token_dir:
        QUOTA_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(args.token_dir)), "quota_state.json")
    _quota_load()
    if QUOTA_STATE_FILE:
        print(f"[gateway] quota state file: {QUOTA_STATE_FILE} ({len(_quota_est)} entries loaded)", flush=True)
    global SESSION_MAP_FILE
    if args.session_map_file:
        SESSION_MAP_FILE = args.session_map_file
    elif args.token_dir:
        SESSION_MAP_FILE = os.path.join(os.path.dirname(os.path.abspath(args.token_dir)), "session_map.json")
    _sessions_load()
    Handler.force_tool_choice = args.force_tool_choice
    Handler.shell_hint = args.shell_hint
    Handler.repeat_guard = args.repeat_guard
    Handler.force_non_stream = args.force_non_stream
    Handler.filter_empty_edit = args.filter_empty_edit
    Handler.exhaust_ban_hours = args.exhaust_ban_hours
    Handler.port = args.port
    with _stats_lock:
        _stats["started_at"] = time.time()
    ThreadingHTTPServer.request_queue_size = 128   # 并发 agent 请求排队容量
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    if args.control_port:
        from threading import Thread
        ctl = ThreadingHTTPServer(("127.0.0.1", args.control_port), ControlHandler)
        Thread(target=ctl.serve_forever, daemon=True).start()
        print(f"[gateway] control api on http://127.0.0.1:{args.control_port}/status", flush=True)

    def _flush_loop():
        while True:
            time.sleep(60)
            with _pool_lock:
                _quota_save(force=True)

    Thread(target=_flush_loop, daemon=True).start()
    threading.Thread(target=_background_probe_loop, daemon=True).start()
    print(f"[gateway] background quota probe enabled (scan every {PROBE_SCAN_INTERVAL}s)", flush=True)
    print(f"grok gateway listening on http://127.0.0.1:{args.port} -> {Handler.upstream}", flush=True)
    print(f"tokens: {[lb for _, lb, _, _ in entries]}", flush=True)
    srv.serve_forever()

if __name__ == '__main__':
    main()