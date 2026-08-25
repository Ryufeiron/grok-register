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
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request

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
    acceptable = {"queued", "waiting", "pending", "requested", "in_progress"}
    for _ in range(18):
        time.sleep(10)
        now = api("/actions/runs?per_page=6")
        if now and now["workflow_runs"]:
            for r in now["workflow_runs"]:
                if r.get("name") == "Run Register Probe" and r.get("event") == "push" \
                        and r.get("status") in acceptable:
                    log(f"register run found: id={r['id']} status={r['status']}")
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
    """用 gh CLI 下载 run 的 register-artifacts 到 dest_dir。

    gh run download 会将 artifact **解压**到 -D 目录（不产生 zip），
    返回找到的 xai-*.json 文件路径列表。
    """
    gh = os.environ.get("GRK_GH_EXE")
    if not gh:
        local_gh = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gh", "gh.exe")
        gh = local_gh if os.path.isfile(local_gh) else "gh"
    env = dict(os.environ)
    if token and not env.get("GH_TOKEN"):
        env["GH_TOKEN"] = token
    os.makedirs(dest_dir, exist_ok=True)
    try:
        r = subprocess.run(
            [gh, "run", "download", str(run_id), "-R", FORK, "-n", "register-artifacts",
             "-D", dest_dir],
            capture_output=True, text=True, timeout=600, env=env,
        )
        if r.returncode != 0:
            log(f"gh run download failed: rc={r.returncode} err={r.stderr[-500:]}")
            return []
    except Exception as e:
        log(f"gh run download error: {e!r}")
        return []
    token_files = []
    for root, _dirs, files in os.walk(dest_dir):
        for fname in files:
            if fname.startswith("xai-") and fname.endswith(".json") and "cpa_auth" in root.replace("\\", "/"):
                token_files.append(os.path.join(root, fname))
    log(f"artifact download ok: {len(token_files)} token file(s)")
    return token_files


def collect_tokens_from_files(paths):
    """读取解压出的 cpa_auth/*.json 文件，返回 {email: token_dict}。"""
    found = {}
    for p in paths:
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("access_token"):
                email = data.get("email") or os.path.basename(p)
                found[email] = data
        except Exception as e:
            log(f"token file parse failed {p}: {e!r}")
    log(f"collected {len(found)} valid token(s) from {len(paths)} file(s)")
    return found


def sync_account_files(acct_dir, token_dir):
    """把 cpa_auth 里每个 token 对应的账号文件对齐到 data/accounts/ 目录。

    本地已有 data/accounts/*.txt 若与 cpa_auth token 邮箱一致则跳过；
    缺失的从 cpa_auth JSON（含 email/sso）生成，保证平台账号库可补录。
    """
    created = []
    os.makedirs(acct_dir, exist_ok=True)
    for name in sorted(os.listdir(token_dir)):
        if not name.endswith(".json"):
            continue
        try:
            data = json.load(open(os.path.join(token_dir, name), encoding="utf-8"))
        except Exception:
            continue
        email = str(data.get("email") or "").strip()
        if not email or "@" not in email:
            continue
        acct_file = os.path.join(acct_dir, f"{email}.txt")
        if os.path.isfile(acct_file):
            continue
        password = str(data.get("password") or "generated-unsaved")
        sso = str(data.get("sso") or "")
        with open(acct_file, "w", encoding="utf-8") as f:
            f.write(f"{email}----{password}----{sso}\n")
        created.append(email)
    if created:
        log(f"sync_account_files: created {len(created)} account file(s): {', '.join(e.split('@')[0] for e in created)}")
    return created


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
    """杀掉旧 gateway 进程并用完整参数重启（Popen 方式，避免 PowerShell 重定向阻塞）。"""
    subprocess.run([
        "powershell", "-NoProfile", "-Command",
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -like '*grok_gateway*' } | "
        "ForEach-Object { taskkill /PID $_.ProcessId /F 2>$null }"],
        capture_output=True, text=True, timeout=60)
    time.sleep(2)
    gw_log = os.path.join(REPO_DIR, "data", "gw.log")
    gw_err = os.path.join(REPO_DIR, "data", "gw_err.log")
    try:
        out_f = open(gw_log, "ab", buffering=0)
        err_f = open(gw_err, "ab", buffering=0)
    except Exception as e:
        log(f"open gateway log failed: {e!r}")
        return False
    cmd = [
        sys.executable, "-u", GATEWAY_PY,
        "--token-dir", TOKEN_DIR,
        "--port", str(PORT),
        "--ban-seconds", "90",
        "--force-tool-choice",
        "--filter-empty-edit",
        "--force-non-stream",
        "--shell-hint",
        "--control-port", "40201",
    ]
    try:
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NO_WINDOW | getattr(subprocess, "DETACHED_PROCESS", 0)
        subprocess.Popen(cmd, cwd=REPO_DIR, stdout=out_f, stderr=err_f,
                         close_fds=True, creationflags=flags)
    except Exception as e:
        log(f"spawn gateway failed: {e!r}")
        return False
    # 验证控制端口
    for _ in range(15):
        time.sleep(1)
        try:
            with urllib.request.urlopen("http://127.0.0.1:40201/status", timeout=2) as resp:
                if resp.status == 200:
                    log("gateway restarted, control port OK")
                    return True
        except Exception:
            pass
    log("gateway restarted, but control port not responding")
    return False


def finalize_run(run_id: int) -> int:
    """等待指定 run 完成 -> 下载 artifact -> 合并 token -> 重启网关。"""
    token = get_github_token()
    if not token:
        log("WARNING: no github credential; artifact download will fail")

    run = wait_run(run_id)
    if not run:
        return 1
    if run.get("conclusion") != "success":
        log("run not success; cannot download artifacts reliably")

    tmp = tempfile.mkdtemp(prefix="daily_token_")
    token_files = fetch_artifacts(run_id, token, tmp) if token else []
    if not token_files:
        log("no token files downloaded from artifact")
        return 1

    new_tokens = collect_tokens_from_files(token_files)
    if not new_tokens:
        log("no valid tokens found in artifact files")
        return 1
    added = merge_tokens(TOKEN_DIR, new_tokens)
    sync_account_files(os.path.join(REPO_DIR, "data", "accounts"), TOKEN_DIR)

    restart_gateway()

    after_count = len([f for f in os.listdir(TOKEN_DIR) if f.endswith('.json')]) if os.path.isdir(TOKEN_DIR) else 0
    log(f"after: {after_count} tokens (added {len(added)})")
    log("daily token refresh DONE")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--now", action="store_true", help="立即触发并等待一轮")
    ap.add_argument("--dry-run", action="store_true", help="只触发+汇报，不下载不合并")
    ap.add_argument("--attach", type=int, default=None, metavar="RUN_ID",
                    help="接管已存在的 run：等待完成并下载合并（不触发新 run）")
    args = ap.parse_args()

    os.makedirs(TOKEN_DIR, exist_ok=True)

    # 接管模式：不触发，直接收尾既有 run
    if args.attach:
        log(f"attach mode: taking over run {args.attach}")
        return finalize_run(args.attach)


    if not args.now and not args.dry_run:
        log("no action flag; use --now to trigger a run now")
        return

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

    # 2-5. 等待/下载/合并/重启
    return finalize_run(run_id)


if __name__ == "__main__":
    sys.exit(main())