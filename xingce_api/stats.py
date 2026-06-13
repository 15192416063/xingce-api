# -*- coding: utf-8 -*-
"""运营统计:在线人数(内存心跳) + 每日 PV/UV/对话/token 计数。
所有写入都吞异常——统计绝不能拖垮业务。"""
import time
import threading
from datetime import date

from db import SessionLocal, StatDaily, VisitDay, TokenStat

_lock = threading.Lock()
ONLINE = {}           # user_id -> 最后活跃时间戳
ONLINE_TTL = 300      # 5分钟内有请求 = 在线


def touch_online(user_id: int):
    ONLINE[user_id] = time.time()


def online_count() -> int:
    now = time.time()
    # 顺手清掉过期条目,防 dict 无限长
    dead = [k for k, t in ONLINE.items() if now - t > ONLINE_TTL * 4]
    for k in dead:
        ONLINE.pop(k, None)
    return sum(1 for t in ONLINE.values() if now - t < ONLINE_TTL)


def _today() -> str:
    return date.today().strftime("%Y-%m-%d")


def _bump(**fields):
    """StatDaily 当日行自增。fields 如 pv=1, chat_count=1, tokens_in=123。"""
    try:
        with _lock:
            db = SessionLocal()
            day = _today()
            row = db.query(StatDaily).filter(StatDaily.day == day).first()
            if not row:
                row = StatDaily(day=day)
                db.add(row)
            for k, v in fields.items():
                setattr(row, k, (getattr(row, k) or 0) + v)
            db.commit()
            db.close()
    except Exception:
        pass


def record_visit(user_id: int = 0):
    """页面打开:PV+1;登录用户记 UV(每人每日一条)。"""
    _bump(pv=1)
    if not user_id:
        return
    try:
        db = SessionLocal()
        day = _today()
        if not db.query(VisitDay).filter(VisitDay.user_id == user_id,
                                         VisitDay.day == day).first():
            db.add(VisitDay(user_id=user_id, day=day))
            db.commit()
        db.close()
    except Exception:
        pass


def record_chat():
    _bump(chat_count=1)


def record_tokens(tokens_in: int, tokens_out: int,
                  scene: str = "其他", channel: str = ""):
    """记 token 消耗:总量进 StatDaily(面板看总成本),
    同时按 (日, 用途, 渠道) 进 TokenStat(面板看「钱花在哪、哪个渠道花的」)。"""
    tin, tout = int(tokens_in or 0), int(tokens_out or 0)
    if not (tin or tout):
        return
    _bump(tokens_in=tin, tokens_out=tout)
    try:
        with _lock:
            db = SessionLocal()
            day = _today()
            row = (db.query(TokenStat)
                   .filter(TokenStat.day == day, TokenStat.scene == scene,
                           TokenStat.channel == channel).first())
            if not row:
                row = TokenStat(day=day, scene=scene, channel=channel)
                db.add(row)
            row.calls = (row.calls or 0) + 1
            row.tokens_in = (row.tokens_in or 0) + tin
            row.tokens_out = (row.tokens_out or 0) + tout
            db.commit()
            db.close()
    except Exception:
        pass
