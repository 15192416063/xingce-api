# -*- coding: utf-8 -*-
"""安全防护:接口限流(滑动窗口) + 登录爆破锁定 + 真实IP + SQLite自动备份。
全部内存实现、零依赖;单进程部署下精确,多进程时各 worker 独立计数(阈值等比放宽即可)。"""
import os
import time
import threading
from collections import deque, defaultdict

import config

_lock = threading.Lock()

# ---------- 滑动窗口限流 ----------
_hits = defaultdict(deque)     # key -> 最近请求时间戳


def allow(key: str, limit: int, window: int = 60) -> bool:
    """窗口内不超 limit 次则放行。"""
    now = time.time()
    with _lock:
        dq = _hits[key]
        while dq and dq[0] < now - window:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(now)
        # 防 key 无限膨胀(被扫描器打出几万个IP时)
        if len(_hits) > 50000:
            for k in [k for k, v in _hits.items() if not v][:10000]:
                _hits.pop(k, None)
        return True


# ---------- 登录爆破锁定 ----------
_fails = {}    # key(user|ip) -> [失败次数, 锁定截止时间戳]


def login_locked(key: str):
    """返回剩余锁定秒数,未锁返回 0。"""
    rec = _fails.get(key)
    if not rec:
        return 0
    left = rec[1] - time.time()
    return int(left) if left > 0 else 0


def login_fail(key: str):
    """记一次失败;达到上限触发锁定。"""
    with _lock:
        rec = _fails.setdefault(key, [0, 0])
        rec[0] += 1
        if rec[0] >= config.LOGIN_MAX_FAILS:
            rec[1] = time.time() + config.LOGIN_LOCK_SECS
            rec[0] = 0


def login_ok(key: str):
    _fails.pop(key, None)


def locked_count() -> int:
    now = time.time()
    return sum(1 for r in _fails.values() if r[1] > now)


# ---------- 真实 IP ----------
def client_ip(request) -> str:
    if config.TRUST_PROXY:
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


# ---------- SQLite 每日自动备份 + 完整性自检(冗余/一致性/防篡改) ----------
INTEGRITY = {"ok": True, "checked": "", "last_backup": ""}


def integrity_status():
    return dict(INTEGRITY)


def start_backup_thread():
    if not config.BACKUP_KEEP or not config.DB_URL.startswith("sqlite"):
        return
    src = config.DB_URL[len("sqlite:///"):]
    bdir = os.path.join(config.BASE_DIR, "backups")

    def run():
        import sqlite3
        from datetime import date, datetime
        while True:
            try:
                if os.path.exists(src):
                    # 1) 完整性自检:库文件损坏/被改坏第一时间在管理面板亮红灯
                    try:
                        c = sqlite3.connect(src)
                        r = c.execute("PRAGMA quick_check").fetchone()
                        c.close()
                        INTEGRITY["ok"] = (r and r[0] == "ok")
                        INTEGRITY["checked"] = datetime.now().strftime("%m-%d %H:%M")
                    except Exception:
                        INTEGRITY["ok"] = False
                    # 2) 每日在线备份(不锁业务)
                    os.makedirs(bdir, exist_ok=True)
                    dst = os.path.join(bdir, f"xingce-{date.today()}.db")
                    if not os.path.exists(dst):
                        s = sqlite3.connect(src)
                        d = sqlite3.connect(dst)
                        s.backup(d)
                        d.close()
                        s.close()
                        olds = sorted(f for f in os.listdir(bdir)
                                      if f.startswith("xingce-") and f.endswith(".db"))
                        for f in olds[:-config.BACKUP_KEEP]:
                            os.remove(os.path.join(bdir, f))
                    backs = sorted(f for f in os.listdir(bdir)
                                   if f.startswith("xingce-")) if os.path.isdir(bdir) else []
                    INTEGRITY["last_backup"] = backs[-1][7:-3] if backs else ""
            except Exception:
                pass
            time.sleep(3600)     # 每小时检查,每天实际只备一次

    threading.Thread(target=run, daemon=True).start()
