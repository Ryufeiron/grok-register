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
PROBE_URL = f"http://127.0.0.1:{CONTROL_PORT}/probe"

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
                "quota_remaining": t.get("quota_remaining"),
                "quota_actual": t.get("quota_actual"),
                "quota_limit": t.get("quota_limit"),
                "quota_updated_at": t.get("quota_updated_at"),
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
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NO_WINDOW
        r = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                "Where-Object { $_.CommandLine -like '*grok_gateway*' }).Count",
            ],
            capture_output=True, text=True, timeout=30, creationflags=flags,
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
        "--force-non-stream",
        "--shell-hint",
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
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW
    subprocess.run(
        [
            "powershell", "-NoProfile", "-Command",
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
            "Where-Object { $_.CommandLine -like '*grok_gateway*' } | "
            "ForEach-Object { taskkill /PID $_.ProcessId /F 2>$null }",
        ],
        capture_output=True, text=True, timeout=30, creationflags=flags,
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


def probe_tokens() -> Dict[str, Any]:
    """同步触发网关全池额度探测（耗时较长，逐 token 探测）。"""
    if not _http_get_json(STATUS_URL, timeout=2.0):
        return {"ok": False, "error": "网关未运行"}
    result = _http_post(PROBE_URL, timeout=600)
    if not result:
        return {"ok": False, "error": "探测请求失败或超时"}
    return {"ok": result.get("ok", False), "summary": result.get("summary"),
            "revived": result.get("revived"), "exhausted": result.get("exhausted"),
            "revoked": result.get("revoked"), **gateway_status()}


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
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NO_WINDOW
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=7200,
                           cwd=str(PROJECT_ROOT), encoding="utf-8", errors="replace",
                           creationflags=flags)
        summary["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
        summary["returncode"] = r.returncode
        tail = (r.stdout or "")[-3000:].strip().splitlines()
        summary["stdout_tail"] = tail[-40:]
        tail_err = (r.stderr or "")[-1500:].strip().splitlines()
        summary["stderr_tail"] = tail_err[-10:]
        summary["ok"] = r.returncode == 0
        summary["duration_s"] = int(time.time() - started)
    except subprocess.TimeoutExpired:
        summary["error"] = "timeout(7200s)"
    except Exception as exc:
        summary["error"] = repr(exc)
    with _TASK["lock"]:
        _TASK["running"] = False
        _TASK["last_result"] = summary
    _log(f"register task done: ok={summary.get('ok')} rc={summary.get('returncode')}")


def _run_daily_attach(run_id: int) -> None:
    """接管既有 run：等待完成 -> 下载合并 -> 重启网关（不触发新 run）。"""
    started = time.time()
    summary = {"ok": False, "started": time.strftime("%Y-%m-%d %H:%M:%S"), "attached_run_id": run_id}
    try:
        cmd = [sys.executable, str(DAILY_PY), "--attach", str(run_id)]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=7200,
                           cwd=str(PROJECT_ROOT), encoding="utf-8", errors="replace",
                           creationflags=flags)
        summary["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
        summary["returncode"] = r.returncode
        tail = (r.stdout or "")[-3000:].strip().splitlines()
        summary["stdout_tail"] = tail[-40:]
        tail_err = (r.stderr or "")[-1500:].strip().splitlines()
        summary["stderr_tail"] = tail_err[-10:]
        summary["ok"] = r.returncode == 0
        summary["duration_s"] = int(time.time() - started)
    except subprocess.TimeoutExpired:
        summary["error"] = "timeout(7200s)"
    except Exception as exc:
        summary["error"] = repr(exc)
    if summary.get("ok"):
        try:
            LAST_MERGED_MARKER.parent.mkdir(parents=True, exist_ok=True)
            LAST_MERGED_MARKER.write_text(str(run_id), encoding="utf-8")
            _log(f"watchdog: run {run_id} merged, marker updated")
        except Exception:
            pass
    with _TASK["lock"]:
        _TASK["running"] = False
        _TASK["last_result"] = summary
    _log(f"attach task done: ok={summary.get('ok')} rc={summary.get('returncode')}")


def _read_last_merged() -> str:
    try:
        return LAST_MERGED_MARKER.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _register_watchdog_loop() -> None:
    """看门狗：发现未被接管的 Run Register Probe run 时自动起 attach 线程收尾。

    覆盖场景：平台重启导致 _run_daily 跟踪线程丢失、外部 push 触发的 run。
    """
    while True:
        time.sleep(WATCHDOG_INTERVAL)
        try:
            if _TASK["running"]:
                continue
            info = _actions_run_status()
            if not info:
                continue
            rid = info.get("run_id")
            if not rid:
                continue
            if str(rid) == _read_last_merged():
                continue
            status = info.get("status")
            if status in ("queued", "waiting", "pending", "requested", "in_progress"):
                # 未完成：立即起 attach 线程等待收尾（wait_run 会阻塞到完成）
                with _TASK["lock"]:
                    busy = _TASK["running"]
                    if not busy:
                        _TASK["running"] = True
                if busy:
                    continue
                _log(f"watchdog: attaching in-flight run {rid} (status={status})")
                threading.Thread(target=_run_daily_attach, args=(int(rid),), daemon=True).start()
                continue
            # 已完成：success 才合并；失败也写 marker 防止反复处理
            if info.get("conclusion") == "success":
                with _TASK["lock"]:
                    busy = _TASK["running"]
                    if not busy:
                        _TASK["running"] = True
                if busy:
                    continue
                _log(f"watchdog: attaching finished run {rid} (conclusion=success)")
                threading.Thread(target=_run_daily_attach, args=(int(rid),), daemon=True).start()
            else:
                try:
                    LAST_MERGED_MARKER.write_text(str(rid), encoding="utf-8")
                    _log(f"watchdog: run {rid} conclusion={info.get('conclusion')}, marked skipped")
                except Exception:
                    pass
        except Exception as exc:
            _log(f"watchdog error: {exc!r}")


def _task_snapshot() -> Dict[str, Any]:
    with _TASK["lock"]:
        snap = {
            "running": bool(_TASK["running"]),
            "last_result": _TASK["last_result"],
        }
    if not snap["running"]:
        snap["actions"] = _actions_run_status()
    return snap


# ------------- GitHub Actions run 状态查询（本地任务丢失时的兜底展示） -------------

ACTIONS_API = "https://api.github.com/repos/Ryufeiron/grok-register/actions/runs"
RUN_NAME = "Run Register Probe"
_actions_cache: Dict[str, Any] = {"at": 0.0, "data": None}
LAST_MERGED_MARKER = PROJECT_ROOT / "data" / "last_merged_run.txt"
WATCHDOG_INTERVAL = 300          # 看门狗扫描间隔（秒）
WATCHDOG_FORCE = os.environ.get("GROK_WATCHDOG_FORCE")  # 调试用


def _actions_run_status(force: bool = False) -> Optional[Dict[str, Any]]:
    now = time.time()
    if not force and _actions_cache["data"] is not None and now - _actions_cache["at"] < 120:
        return _actions_cache["data"]
    result = None
    try:
        req = urllib.request.Request(
            ACTIONS_API + "?per_page=10",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "grok-register-web"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        for run in data.get("workflow_runs", []):
            if run.get("name") != RUN_NAME:
                continue
            result = {
                "run_id": run.get("id"),
                "status": run.get("status"),          # queued / in_progress / completed
                "conclusion": run.get("conclusion"),  # success / failure / null
                "created_at": run.get("created_at"),
                "updated_at": run.get("updated_at"),
                "html_url": run.get("html_url"),
                "source": "github_actions",
            }
            break
    except Exception as exc:
        _log(f"query actions status failed: {exc!r}")
    _actions_cache["at"] = now
    _actions_cache["data"] = result
    return result

# ------------- 每日注册调度配置（同步 Windows 计划任务） -------------

SCHEDULE_FILE = PROJECT_ROOT / "data" / "register_schedule.json"
SCHEDULE_TASK_NAME = "GrokTokenDailyRegister"

DEFAULT_SCHEDULE = {
    "enabled": True,
    "time": "22:00",
    "random_delay_max_min": 120,
}


def _read_schedule() -> Dict[str, Any]:
    cfg = dict(DEFAULT_SCHEDULE)
    try:
        if SCHEDULE_FILE.exists():
            data = json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
            cfg.update(data)
    except Exception:
        pass
    return cfg


def _write_schedule(cfg: Dict[str, Any]) -> None:
    try:
        SCHEDULE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SCHEDULE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(SCHEDULE_FILE)
    except Exception as exc:
        _log(f"write schedule failed: {exc!r}")


def _schtasks(cmd: list) -> None:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=30, creationflags=flags)
    except Exception as exc:
        _log(f"schtasks failed: {exc!r}")


def _apply_schedule(cfg: Dict[str, Any]) -> None:
    """把配置同步到 Windows 计划任务（存在则改时间/启停，不存在则创建）。"""
    if os.name != "nt":
        return
    hour, minute = "22", "00"
    try:
        hhmm = str(cfg.get("time", "22:00")).strip()
        hour, minute = hhmm.split(":")[:2]
    except Exception:
        pass
    if cfg.get("enabled", True):
        _schtasks([
            "schtasks", "/Create", "/F", "/TN", SCHEDULE_TASK_NAME,
            "/SC", "DAILY", "/ST", f"{hour}:{minute}",
            "/TR", f'\"{sys.executable}\" \"{DAILY_PY}\"',
        ])
    else:
        _schtasks(["schtasks", "/Delete", "/F", "/TN", SCHEDULE_TASK_NAME])


def get_schedule() -> Dict[str, Any]:
    cfg = _read_schedule()
    info = {"task_exists": False, "task_state": None}
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["schtasks", "/Query", "/TN", SCHEDULE_TASK_NAME, "/FO", "LIST"],
                capture_output=True, text=True, timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            ).stdout
            info["task_exists"] = SCHEDULE_TASK_NAME in out
            info["task_state"] = "enabled" if ("Ready" in out or "启用" in out) else ("exists" if info["task_exists"] else None)
        except Exception:
            pass
    return {**cfg, **info}


def set_schedule(payload: Dict[str, Any]) -> Dict[str, Any]:
    cfg = _read_schedule()
    if "enabled" in payload:
        cfg["enabled"] = bool(payload["enabled"])
    if "time" in payload and isinstance(payload.get("time"), str):
        cfg["time"] = payload["time"].strip()
    if "random_delay_max_min" in payload:
        try:
            cfg["random_delay_max_min"] = max(0, int(payload["random_delay_max_min"]))
        except Exception:
            pass
    _write_schedule(cfg)
    _apply_schedule(cfg)
    _log(f"schedule updated: {cfg}")
    return {**cfg, "ok": True}


threading.Thread(target=_register_watchdog_loop, name="register-watchdog", daemon=True).start()
