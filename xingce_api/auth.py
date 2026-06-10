# -*- coding: utf-8 -*-
"""认证与安全:密码哈希(PBKDF2-HMAC-SHA256 + 每用户盐) + 签名令牌(HMAC)。
全部用标准库,无第三方依赖,部署简单。"""
import os
import hmac
import json
import time
import base64
import hashlib

from fastapi import Header, HTTPException

import config
from db import SessionLocal, User

_ITER = 200_000  # PBKDF2 迭代次数


# ---------- 密码 ----------
def hash_password(password: str) -> str:
    """返回 'salt$hash'(都是hex)。每个用户独立随机盐。"""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITER)
    return salt.hex() + "$" + dk.hex()


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, hash_hex = stored.split("$", 1)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                 bytes.fromhex(salt_hex), _ITER)
        return hmac.compare_digest(dk.hex(), hash_hex)  # 防时序攻击
    except Exception:
        return False


# ---------- 令牌(自签 JWT 风格) ----------
def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_token(user_id: int, role: int) -> str:
    payload = {"uid": user_id, "role": role,
               "exp": int(time.time()) + config.TOKEN_TTL_DAYS * 86400}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(config.SECRET_KEY.encode(), body.encode(),
                        hashlib.sha256).digest())
    return body + "." + sig


def parse_token(token: str):
    try:
        body, sig = token.split(".", 1)
        expect = _b64(hmac.new(config.SECRET_KEY.encode(), body.encode(),
                               hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expect):
            return None
        payload = json.loads(_unb64(body))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


# ---------- FastAPI 依赖 ----------
def _user_from_header(authorization: str):
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    payload = parse_token(authorization[7:])
    if not payload:
        return None
    db = SessionLocal()
    u = db.get(User, payload["uid"])
    db.close()
    return u


def current_user(authorization: str = Header(default="")):
    """登录用户(必须)。未登录抛401。"""
    u = _user_from_header(authorization)
    if not u or u.status != 1:
        raise HTTPException(401, "未登录或登录已过期")
    return u


def require_admin(authorization: str = Header(default="")):
    u = _user_from_header(authorization)
    if not u or u.role != 1:
        raise HTTPException(403, "需要管理员权限")
    return u


def optional_user(authorization: str = Header(default="")):
    """可选登录(资讯等公开接口用)"""
    return _user_from_header(authorization)
