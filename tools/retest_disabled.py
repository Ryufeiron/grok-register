# -*- coding: utf-8 -*-
"""复核 data/cpa_auth_disabled/ 中被吊销怀疑的 token：
refresh 成功(200) -> 移回主池复活；仍 invalid_grant(400) -> 保持归档。
用法: python tools/retest_disabled.py
"""
import http.client
import json
import os
import ssl
import sys
import time
import urllib.parse

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(ROOT, "data", "cpa_auth")
DIS = os.path.join(ROOT, "data", "cpa_auth_disabled")
CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
SCOPE = "openid profile email offline_access grok-cli:access api:access"


def refresh(rt):
    params = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "refresh_token": rt,
        "scope": SCOPE,
    })
    conn = http.client.HTTPSConnection("auth.x.ai", 443, context=ssl.create_default_context(), timeout=30)
    conn.request("POST", "/oauth2/token", body=params, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "grok-pager/0.1.202"})
    resp = conn.getresponse()
    body = resp.read().decode("utf-8", "replace")
    conn.close()
    return resp.status, body


def main():
    if not os.path.isdir(DIS):
        print("no disabled dir, nothing to do")
        return
    revived = []
    revoked = []
    for name in sorted(os.listdir(DIS)):
        p = os.path.join(DIS, name)
        if not name.endswith(".json"):
            continue
        try:
            data = json.load(open(p, encoding="utf-8"))
            rt = data.get("refresh_token", "")
            email = data.get("email", name)
        except Exception as e:
            print(f"read fail {name}: {e!r}")
            continue
        if not rt:
            revoked.append((name, "no refresh_token"))
            print(f"{email[:26]:28s} no refresh_token, keep")
            continue
        status, body = refresh(rt)
        if status == 200:
            try:
                nj = json.loads(body)
                data["access_token"] = nj["access_token"]
                data["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"write fail {name}: {e!r}")
            shutil_move(p, os.path.join(MAIN, name))
            revived.append(name)
            print(f"{email[:26]:28s} -> 200 REVIVED, moved back")
        else:
            revoked.append((name, str(status)))
            print(f"{email[:26]:28s} -> {status} {body[:80].strip()!r} keep archived")
        time.sleep(1.0)
    print("=" * 40)
    print(f"revived: {len(revived)}  revoked: {len(revoked)}")
    if revived:
        print("网关需重启以加载新 token（gw_restart.py）")


def shutil_move(src, dst):
    import shutil
    shutil.move(src, dst)


if __name__ == "__main__":
    main()