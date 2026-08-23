"""
daily_grok_token_free.py
每日自动补充 grok 免费 token 池：
 1. git push 空 commit 触发 fork 仓库的 GitHub Actions 注册 workflow
 2. 轮询 Actions run 状态（无认证 API 可读）
 3. run 完成后用 git 凭据（git credential fill）下载 artifact zip
 4. 解压合并新的 cpa_auth/*.json 到 data/cpa_auth/
 5. 重启本机 grok 网关（:40200）
 6. 打印结果摘要

用法:  python tools/daily_grok_token_free.py [--now] [--dry-run]
可选 --now: 立即触发并等待一轮完成
       --dry-run: 只触发+汇报，不下载不合并
"""
import argparse
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TOKEN_DIR = os.path.join(REPO_DIR, "data", "cpa_auth")
FORK = "Ryufeiron/grok-register"
REMOTE = f"https://github.com/{FORK}.git"
API = f"https://api.github.com/repos/{FORK}"
LOG_FILE = os.path.join(REPO_DIR, "data", "daily_token_free.log")
GATEWAY_PY = os.path.join(REPO_DIR, "tools", "grok_gateway.py")
PORT = 40200

GITHUB_TOKEN = None


def log(msg):
    line = time.strftime("[%Y-%m-%d %H:%M:%S] ") + str(msg)
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def git(args, cwd=None, capture=True):
    r = subprocess.run(["git"] + args, cwd=cwd or REPO_DIR, capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def get_github_token():
    global GITHUB_TOKEN
    if GITHUB_TOKEN:
        return GITHUB_TOKEN
    try:
        r = subprocess.run(["git", "credential", "fill"], input="url=https://github.com\n",
                           capture_output=True, text=True, cwd=REPO_DIR, timeout=30)
        for line in r.stdout.splitlines():
            if line.startswith("password="):
                GITHUB_TOKEN = line[len("password="):].strip()
                return GITHUB_TOKEN
    except Exception as e:
        log(f"git credential fill failed: {e!r}")
    return None


def api(path, token=None):
    req = urllib.request.Request(API + path, headers={
        "User-Agent": "daily-grok-token", "Accept": "application/vnd.github+json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        log(f"api {path} -> HTTP {e.code}: {body}")
        return None
    except Exception as e:
        log(f"api {path} -> ERR {e!r}")
        return None


def trigger_workflow(branch="main"):
    """push 空 commit 触发 workflow。返回目标 register run 的 id 或 None。"""
    rc, out, err = git(["commit", "--allow-empty", "-m", f"daily token run {time.strftime('%Y-%m-%d')}"])
    if rc != 0:
        log(f"empty commit failed: {err}")
        return None
    rc, out, err = git(["push", REMOTE, f"HEAD:main"])
    if rc != 0:
        log(f"push failed: {err}")
        return None
    log("pushed empty commit, waiting for register run to appear")
    for _ in range(12):
        time.sleep(10)
        now = api("/actions/runs?per_page=6")
        if now and now["workflow_runs"]:
            for r in now["workflow_runs"]:
                if r.get("name") == "Run Register Probe" and r.get("event") == "push" \
                        and r.get("status") == "in_progress":
                    log(f"register run found: id={r['id']}")
                    return r["id"]
    log("no register run detected")
    return None


def wait_run(run_id, timeout=4200):
    t0 = time.time()
    while time.time() - t0 < timeout:
        run = api(f"/actions/runs/{run_id}")
        if not run:
            time.sleep(20)
            continue
        if run.get("status") == "completed":
            log(f"run {run_id} completed conclusion={run.get('conclusion')}")
            return run
        time.sleep(30)
    log(f"run {run_id} timeout after {timeout}s")
    return None


def fetch_artifacts(run_id, token, dest_dir):
    """下载 run 的所有 artifact 到 dest_dir，返回 [(name, zip_path), ...]"""
    out = api(f"/actions/runs/{run_id}/artifacts", token=token)
    if not out:
        return []
    arts = out.get("artifacts", [])
    saved = []
    for a in arts:
        url = f"https://api.github.com/repos/{FORK}/actions/artifacts/{a['id']}/zip"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}",
                                                   "User-Agent": "daily-grok-token"})
        zip_path = os.path.join(dest_dir, f"art_{a['id']}.zip")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp, open(zip_path, "wb") as f:
                f.write(resp.read())
            saved.append((a["name"], zip_path))
            log(f"artifact {a['name']} ({a['size_in_bytes']}B) -> {zip_path}")
        except Exception as e:
            log(f"artifact download {a['name']} failed: {e!r}")
    return saved


def collect_tokens_from_artifact(zip_path):
    """在 zip 内找 data/cpa_auth/*.json，返回 [(email, token_json_dict), ...]"""
    found = {}
    try:
        with zipfile.ZipFile(zip_path) as z:
            for n in z.namelist():
                m = re.search(r"/data/cpa_auth/(xai-[^/]+\.json)$", n)
                if m:
                    data = json.loads(z.read(n).decode("utf-8"))
                    email = data.get("email") or m.group(1)
                    if data.get("access_token"):
                        found[email] = data
        log(f"{zip_path}: found {len(found)} tokens")
    except Exception as e:
        log(f"zip parse {zip_path} failed: {e!r}")
    return found


def merge_tokens(token_dir, new_tokens):
    """把新 token 写入 token_dir（按 email 作为文件名），返回新增数量。"""
    added = []
    for email, data in new_tokens.items():
        fname = f"xai-{email}.json"
        path = os.path.join(token_dir, fname)
        if os.path.isfile(path):
            try:
                old = json.load(open(path, encoding="utf-8"))
                if old.get("access_token") == data.get("access_token"):
                    continue
                if old.get("refresh_token") != data.get("refresh_token"):
                    log(f"{email}: refresh_token changed, refreshing file")
                old.update(data)
                data = old
            except Exception:
                pass
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        added.append(email)
    if added:
        log(f"merged {len(added)} new/updated token(s): {', '.join(e.split('@')[0] for e in added)}")
    return added


def restart_gateway():
    """杀掉旧 gateway 进程并用标准参数重启。"""
    ps = subprocess.run([
        "powershell", "-NoProfile", "-Command",
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -like '*grok_gateway*' } | "
        "ForEach-Object { taskkill /PID $_.ProcessId /F 2>$null }"],
        capture_output=True, text=True, timeout=60)
    time.sleep(2)
    gw_log = os.path.join(tempfile.gettempdir(), "opencode", "gw.log")
    gw_err = os.path.join(tempfile.gettempdir(), "opencode", "gw_err.log")
    try:
        os.makedirs(os.path.dirname(gw_log), exist_ok=True)
    except Exception:
        pass
    args = f"'python' '-u' '{GATEWAY_PY}' '--token-dir' '{TOKEN_DIR}' '--port' '{PORT}' '--ban-seconds' '90' '--force-tool-choice'"
    ps2 = subprocess.run([
        "powershell", "-NoProfile", "-Command",
        f"Start-Process python -ArgumentList {args} -WindowStyle Hidden "
        f"-RedirectStandardOutput '{gw_log}' -RedirectStandardError '{gw_err}'"],
        capture_output=True, text=True, timeout=60)
    time.sleep(5)
    # 验证监听
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"(Get-NetTCPConnection -LocalPort {PORT} -State Listen -ErrorAction SilentlyContinue).Count"],
                       capture_output=True, text=True, timeout=30)
    listening = r.stdout.strip() == "1"
    log(f"gateway restarted, listening={listening}")
    return listening


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--now", action="store_true", help="立即触发并等待一轮")
    ap.add_argument("--dry-run", action="store_true", help="只触发+汇报，不下载不合并")
    args = ap.parse_args()

    if not args.now and not args.dry_run:
        log("no action flag; use --now to trigger a run now")
        return

    os.makedirs(TOKEN_DIR, exist_ok=True)
    token = get_github_token()
    if token:
        log("github credential ok")
    else:
        log("WARNING: no github credential; artifact download will fail")

    before_count = len([f for f in os.listdir(TOKEN_DIR) if f.endswith('.json')]) if os.path.isdir(TOKEN_DIR) else 0
    log(f"before: {before_count} tokens")

    # 1. 触发
    run_id = trigger_workflow()
    if not run_id:
        log("no run triggered, abort")
        return 1
    if args.dry_run:
        log("dry-run: skipping wait/download/merge")
        return 0

    # 2. 等待完成
    run = wait_run(run_id)
    if not run:
        return 1
    if run.get("conclusion") != "success":
        log("run not success; cannot download artifacts reliably")

    # 3. 下载 artifact
    tmp = tempfile.mkdtemp(prefix="daily_token_")
    arts = fetch_artifacts(run_id, token, tmp) if token else []
    if not arts:
        log("no artifacts downloaded")
        return 1

    # 4. 合并 token
    new_tokens = {}
    for name, zp in arts:
        new_tokens.update(collect_tokens_from_artifact(zp))
    if not new_tokens:
        log("no valid tokens found in artifacts")
        return 1
    added = merge_tokens(TOKEN_DIR, new_tokens)

    # 5. 重启网关
    restart_gateway()

    after_count = len([f for f in os.listdir(TOKEN_DIR) if f.endswith('.json')]) if os.path.isdir(TOKEN_DIR) else 0
    log(f"after: {after_count} tokens (added {len(added)})")
    log("daily token refresh DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())