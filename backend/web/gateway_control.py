# -*- coding: utf-8 -*-
"""Grok 网关子进程托管与健康监测。

通过 40201 控制端口读取网关状态、控制启停/刷新，并异步执行
tools/daily_grok_token_free.py 注册新 token（手动触发 + 每日调度）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.shared.paths import PROJECT_ROOT

GATEWAY_PY = PROJECT_ROOT / "tools" / "grok_gateway.py"
DAILY_PY = PROJECT_ROOT / "tools" / "daily_grok_token_free.py"
TOKEN_DIR = PROJECT_ROOT / "data" / "cpa_auth"
GATEWAY_PORT = 40200
CONTROL_PORT = 40201
STATUS_URL = f"http://127.0.0.1:{CONTROL_PORT}/status"
REFRESH_URL = f"http://127.0.0.1:{CONTROL_PORT}/refresh"

WARN_THRESHOLD = 0.5      # 可用 token 率低于 50% 告警
LOG_FILE = PROJECT_ROOT / "data" / "gateway_manager.log"

# ------------- 注册任务（daily 脚本）状态 -------------

_TASK = {
    "running": False,
    "last_result": None,   # {ok, started, finished, summary, error}
    "lock": threading.Lock(),
}
_UNLOCK = threading.Lock()   # 防止并发触发

# ------------- 日志 -------------


def _log(msg: str) -> None:
    line = time.strftime("[%Y-%m-%d %H:%M:%S] ") + str(msg)
    print(f"[gateway-manager] {msg}", flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ------------- 网关状态 -------------


def _http_get_json(url: str, timeout: float = 5.0) -> Optional[Dict[str, Any]]:
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None


def _http_post(url: str, timeout: float = 60.0) -> Optional[Dict[str, Any]]:
    try:
        req = urllib.request.Request(url, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None


def gateway_status(snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    snap = snapshot or _http_get_json(STATUS_URL)
    tokens: List[Dict[str, Any]] = []
    healthy = 0
    expired = 0
    cooling = 0
    if snap:
        for t in snap.get("tokens", []):
            is_healthy = bool(not t.get("expired") and not t.get("cooling"))
            if is_healthy:
                healthy += 1
            if t.get("expired"):
                expired += 1
            if t.get("cooling"):
                cooling += 1
            tokens.append({
                "label": t.get("label"),
                "has_refresh": bool(t.get("has_refresh")),
                "expired": bool(t.get("expired")),
                "cooling": bool(t.get("cooling")),
                "cooldown_until": t.get("cooldown_until"),
                "exp_ts": t.get("exp_ts"),
                "count_429": t.get("count_429", 0),
                "count_401": t.get("count_401", 0),
                "count_ok": t.get("count_ok", 0),
            })
    total = len(tokens)
    rate = (healthy / total) if total else 0.0
    warn = total > 0 and rate < WARN_THRESHOLD
    return {
        "ok": bool(snap),
        "running": bool(snap),
        "url": STATUS_URL if snap else None,
        "tokens_total": total,
        "tokens_healthy": healthy,
        "tokens_expired": expired,
        "tokens_cooling": cooling,
        "healthy_rate": round(rate, 3),
        "warn": warn,
        "warn_threshold": WARN_THRESHOLD,
        "tokens": tokens,
        "stats": (snap or {}).get("stats"),
        "upstream": (snap or {}).get("upstream"),
        "port": (snap or {}).get("port"),
        "gateway_active": _gateway_process_active(),
        "task": _task_snapshot(),
    }


# ------------- 进程托管 -------------


def _gateway_process_active() -> bool:
    try:
        r = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                "Where-Object { $_.CommandLine -like '*grok_gateway*' }).Count",
            ],
            capture_output=True, text=True, timeout=30,
        )
        return r.stdout.strip() == "1"
    except Exception:
        return False


def _spawn_gateway() -> bool:
    """以独立子进程启动网关（不经 PowerShell，避免 Start-Process 弹窗挂起）。"""
    gw_log = Path(PROJECT_ROOT) / "data" / "gw.log"
    gw_err = Path(PROJECT_ROOT) / "data" / "gw_err.log"
    try:
        gw_log.parent.mkdir(parents=True, exist_ok=True)
        out_f = open(gw_log, "ab", buffering=0)
        err_f = open(gw_err, "ab", buffering=0)
    except Exception as exc:
        _log(f"open gateway log failed: {exc!r}")
        return False
    cmd = [
        sys.executable, "-u", str(GATEWAY_PY),
        "--token-dir", str(TOKEN_DIR),
        "--port", str(GATEWAY_PORT),
        "--ban-seconds", "90",
        "--force-tool-choice",
        "--filter-empty-edit",
        "--control-port", str(CONTROL_PORT),
    ]
    try:
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NO_WINDOW | getattr(subprocess, "DETACHED_PROCESS", 0)
        subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            stdout=out_f,
            stderr=err_f,
            close_fds=True,
            creationflags=flags,
            start_new_session=True,
        )
    except Exception as exc:
        _log(f"spawn gateway failed: {exc!r}")
        try:
            out_f.close()
            err_f.close()
        except Exception:
            pass
        return False
    for _ in range(12):
        time.sleep(1)
        if _http_get_json(STATUS_URL, timeout=2.0):
            return True
    return False


def _kill_gateway() -> None:
    subprocess.run(
        [
            "powershell", "-NoProfile", "-Command",
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
            "Where-Object { $_.CommandLine -like '*grok_gateway*' } | "
            "ForEach-Object { taskkill /PID $_.ProcessId /F 2>$null }",
        ],
        capture_output=True, text=True, timeout=30,
    )


def start_gateway(force: bool = False) -> Dict[str, Any]:
    if _http_get_json(STATUS_URL, timeout=2.0):
        return {"ok": True, "already_running": True, **gateway_status()}
    if not TOKEN_DIR.is_dir():
        return {"ok": False, "error": f"token 目录不存在: {TOKEN_DIR}"}
    started = _spawn_gateway()
    if not started:
        return {"ok": False, "error": "网关启动失败（未监听控制端口）"}
    return {"ok": True, "already_running": False, **gateway_status()}


def stop_gateway() -> Dict[str, Any]:
    if not _http_get_json(STATUS_URL, timeout=2.0) and not _gateway_process_active():
        return {"ok": True, "already_stopped": True}
    _kill_gateway()
    time.sleep(2)
    return {"ok": not (_gateway_process_active() or _http_get_json(STATUS_URL, timeout=2.0))}


def refresh_tokens() -> Dict[str, Any]:
    if not _http_get_json(STATUS_URL, timeout=2.0):
        return {"ok": False, "error": "网关未运行"}
    result = _http_post(REFRESH_URL, timeout=120)
    if not result:
        return {"ok": False, "error": "刷新请求失败（控制端口无响应）"}
    return {"ok": result.get("ok", False), **gateway_status()}


# ------------- 注册新 token（daily 脚本异步执行） -------------


def register_now() -> Dict[str, Any]:
    """异步触发一次注册流程（--now 模式）。"""
    if _TASK["running"]:
        return {"ok": True, "already_running": True, "task": _task_snapshot()}
    with _UNLOCK:
        if _TASK["running"]:
            return {"ok": True, "already_running": True, "task": _task_snapshot()}
        if not DAILY_PY.exists():
            return {"ok": False, "error": f"daily 脚本不存在: {DAILY_PY}"}
        _TASK["running"] = True
        _TASK["last_result"] = None
        threading.Thread(target=_run_daily, args=(True,), daemon=True).start()
        return {"ok": True, "started": True, "task": _task_snapshot()}


def _run_daily(now: bool) -> None:
    started = time.time()
    summary = {"ok": False, "started": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        cmd = [sys.executable, str(DAILY_PY), "--now"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=7200, cwd=str(PROJECT_ROOT))
        summary["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
        summary["returncode"] = r.returncode
        tail = (r.stdout or "")[-3000:].strip().splitlines()
        summary["stdout_tail"] = tail[-40:]
        tail_err = (r.stderr or "")[-1500:].strip().splitlines()
        summary["stderr_tail"] = tail_err[-10:]
        summary["ok"] = r.returncode == 0
        summary["duration_s"] = int(time.time() - started)
    except Exception as exc:
        summary["error"] = repr(exc)
    except subprocess.TimeoutExpired:
        summary["error"] = "timeout(7200s)"
    with _TASK["lock"]:
        _TASK["running"] = False
        _TASK["last_result"] = summary
    _log(f"register task done: ok={summary.get('ok')} rc={summary.get('returncode')}")


def _task_snapshot() -> Dict[str, Any]:
    with _TASK["lock"]:
        return {
            "running": bool(_TASK["running"]),
            "last_result": _TASK["last_result"],
        }