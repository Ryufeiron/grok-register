# -*- coding: utf-8 -*-
"""每日自动注册启动器：随机延迟 0-2h 后调用 daily_grok_token_free.py。
用法: python tools/daily_launch.py
"""
import random
import subprocess
import sys
import time

random.seed()
delay = random.uniform(0, 7200)
print(f"[daily_launch] random delay {delay:.0f}s", flush=True)
time.sleep(delay)

log = open(r"D:\github\grok-register\data\daily_launch.log", "a", encoding="utf-8")
log.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + f"delay {delay:.0f}s, starting daily\n")
log.flush()

r = subprocess.run(
    [sys.executable, r"D:\github\grok-register\tools\daily_grok_token_free.py", "--now"],
    capture_output=True, text=True, encoding="utf-8", errors="replace",
    cwd=r"D:\github\grok-register",
    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    timeout=3600,
)
log.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") +
          f"daily done rc={r.returncode}\n" + (r.stdout or "")[-2000:] + "\n" + (r.stderr or "")[-500:] + "\n")
log.close()
print(f"[daily_launch] done rc={r.returncode}", flush=True)