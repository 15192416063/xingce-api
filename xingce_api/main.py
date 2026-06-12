# -*- coding: utf-8 -*-
"""行测智能题库 · 后端 API(含账号体系/权限/会员/资讯)。
启动: uvicorn main:app   (在 xingce_api 目录下)
"""
import os
import re
import json
import uuid
import random
from datetime import date, datetime, timedelta

from fastapi import (FastAPI, UploadFile, File, BackgroundTasks, HTTPException,
                     Form, Depends, Body, Request)
from fastapi.staticfiles import StaticFiles
from fastapi.responses import Response, JSONResponse

from sqlalchemy import func

import config
import ingest
import vectors
import textutil
import auth
import ai
import pdfgen
import stats as opstats
import security
from db import (init_db, SessionLocal, IngestionJob, Question, QuestionImage,
                PracticeRecord, WrongBook, MaterialGroup, Favorite, User, News,
                AiUsage, StatDaily, VisitDay, LoginLog, Setting, AuditLog, AiChannel,
                Paper, InviteCode, ExamInfo, MockExam)
import exams

# ---- 错误日志:轮转文件,排障不靠猜(稳定性) ----
import logging
import traceback
from logging.handlers import RotatingFileHandler

os.makedirs(os.path.join(config.BASE_DIR, "logs"), exist_ok=True)
_logger = logging.getLogger("xc")
_logger.setLevel(logging.INFO)
_h = RotatingFileHandler(os.path.join(config.BASE_DIR, "logs", "app.log"),
                         maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
_h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
_logger.addHandler(_h)

app = FastAPI(title="行测智能题库 API", version="2.2",
              docs_url=None, redoc_url=None, openapi_url=None)  # 生产关掉接口文档
init_db()
security.start_backup_thread()

# ---- 可在管理面板热改的配置(白名单;改其它键一律拒绝) ----
RUNTIME_SETTINGS = {
    "DEEPSEEK_API_KEY": ("DEEPSEEK_API_KEY", True),   # (config属性名, 是否敏感)
    "XC_EMBED_KEY": ("EMBED_KEY", True),
    "XC_EMBED_BASE_URL": ("EMBED_BASE_URL", False),
    "XC_EMBED_MODEL": ("EMBED_MODEL", False),
    "XC_EXAM_NAME": ("EXAM_NAME", False),
    "XC_EXAM_DATE": ("EXAM_DATE", False),
    "XC_INVITE_REQUIRED": ("INVITE_REQUIRED", False),   # "true"/"false"
}


def _apply_setting(skey: str, sval: str):
    attr, _ = RUNTIME_SETTINGS[skey]
    if skey == "XC_INVITE_REQUIRED":
        setattr(config, attr, sval.strip().lower() == "true")
        return
    setattr(config, attr, sval)
    if skey == "DEEPSEEK_API_KEY":
        ai._llm.cache_clear()        # 换 key 后重建 LLM 客户端


def _startup_recover():
    """启动自愈:1) 数据库里的配置覆盖生效;2) 上次解析到一半的任务标失败(一致性)。"""
    db = SessionLocal()
    for s in db.query(Setting).all():
        if s.skey in RUNTIME_SETTINGS and s.sval:
            try:
                _apply_setting(s.skey, s.sval)
            except Exception:
                pass
    stuck = db.query(IngestionJob).filter(IngestionJob.status.in_([0, 1, 2])) \
        .update({IngestionJob.status: 4,
                 IngestionJob.error_msg: "服务重启中断,请重新上传"},
                synchronize_session=False)
    db.commit()
    db.close()
    if stuck:
        _logger.info("启动自愈:清理中断任务 %s 个", stuck)


_startup_recover()
try:
    exams.seed()    # 全国考试日历内置数据(幂等)
except Exception:
    _logger.exception("考试日历种子数据写入失败")


def _audit(user_id: int, action: str, detail: str = ""):
    """管理员操作留痕(防篡改:有据可查)"""
    try:
        db = SessionLocal()
        db.add(AuditLog(user_id=user_id, action=action, detail=detail[:512]))
        db.commit()
        db.close()
    except Exception:
        pass


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    """兜底:任何未捕获异常记日志、给用户友好提示,绝不泄露堆栈。"""
    _logger.error("500 %s %s\n%s", request.url.path, exc, traceback.format_exc())
    return JSONResponse({"detail": "服务器开小差了,请稍后重试"}, status_code=500)


@app.middleware("http")
async def _security_mw(request, call_next):
    """限流(防刷/防爆破) + 安全响应头 + HTML 不缓存,一个中间件全做了。"""
    p = request.url.path
    if p.startswith("/api/"):
        ip = security.client_ip(request)
        # 三层限流:认证类最严(防爆破),AI类次之(防烧钱),全局兜底(防洪水)
        if p.startswith("/api/auth/") and not security.allow(
                f"a:{ip}", config.RL_AUTH_PER_MIN):
            return JSONResponse({"detail": "操作太频繁,请稍后再试"}, status_code=429)
        if (p.startswith("/api/ai/") or p == "/api/mine/add") and not security.allow(
                f"i:{ip}", config.RL_AI_PER_MIN):
            return JSONResponse({"detail": "AI 请求太频繁,歇一会儿~"}, status_code=429)
        if not security.allow(f"g:{ip}", config.RL_GLOBAL_PER_MIN):
            return JSONResponse({"detail": "请求过于频繁"}, status_code=429)
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"           # 防点击劫持
    resp.headers["Referrer-Policy"] = "no-referrer"
    if p == "/" or p.endswith(".html"):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
    return resp


app.mount("/img", StaticFiles(directory=config.IMAGE_DIR), name="img")


# ============ 工具 ============
def _q_to_vo(q: Question, db, with_answer=False, user_id=0):
    imgs = db.query(QuestionImage).filter(QuestionImage.question_id == q.id).all()
    image_urls = [f"/img/{i.object_key}".replace("\\", "/") for i in imgs]
    faved = bool(user_id) and db.query(Favorite).filter(
        Favorite.user_id == user_id, Favorite.question_id == q.id).first() is not None
    vo = {
        "id": q.id, "source": q.source, "seq_no": q.seq_no,
        "category_l1": q.category_l1, "category_l2": q.category_l2,
        "knowledge_point": q.knowledge_point, "difficulty": q.difficulty,
        "content": textutil.clean_text(q.content),
        "has_image": q.has_image, "confidence": q.confidence,
        "status": q.status, "favorited": faved,
        "mine": q.scope.startswith("user:"),
        "material_text": "", "material_images": [], "images": image_urls,
    }
    if q.material_id:
        mg = db.get(MaterialGroup, q.material_id)
        if mg:
            vo["material_text"] = textutil.clean_text(mg.material_text)
            vo["material_images"] = [f"/img/{k}".replace("\\", "/")
                                     for k in mg.image_keys.split("|") if k]
    if with_answer:
        vo["answer"] = q.answer
        vo["answer_origin"] = q.answer_origin
        vo["explanation"] = q.explanation
    return vo


def _ai_guard(u: User):
    """每人每日 AI 调用硬上限(管理员不限)。超限抛 429。返回今日用量。"""
    if u.role == 1:
        return 0
    today = date.today().strftime("%Y-%m-%d")
    db = SessionLocal()
    rec = db.query(AiUsage).filter(AiUsage.user_id == u.id, AiUsage.day == today).first()
    used = rec.count if rec else 0
    if used >= config.AI_DAILY_CAP:
        db.close()
        raise HTTPException(429, f"今日 AI 使用已达上限({config.AI_DAILY_CAP}次),明天再来吧~")
    if rec:
        rec.count += 1
    else:
        db.add(AiUsage(user_id=u.id, day=today, count=1))
    db.commit()
    db.close()
    return used + 1


def _user_vo(u: User):
    return {"id": u.id, "username": u.username, "nickname": u.nickname or u.username,
            "role": u.role}


# 错题间隔重复:做对一次升档,档位间隔 1/3/7/15 天,全过自动标掌握
_REVIEW_GAPS = [1, 3, 7, 15]


def _update_wrongbook(db, uid: int, qid: int, correct):
    """correct: True/False/None(未判分)。错→进错题本明天复习;对→升档。"""
    if correct is None:
        return
    wb = db.query(WrongBook).filter(WrongBook.user_id == uid,
                                    WrongBook.question_id == qid).first()
    if correct is False:
        if wb:
            wb.wrong_count += 1
            wb.mastered = 0
            wb.box = 0
        else:
            wb = WrongBook(user_id=uid, question_id=qid, box=0)
            db.add(wb)
        wb.next_review = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")
    elif wb and not wb.mastered:
        wb.box = (wb.box or 0) + 1
        if wb.box >= len(_REVIEW_GAPS):
            wb.mastered = 1
            wb.next_review = ""
        else:
            wb.next_review = (date.today() + timedelta(
                days=_REVIEW_GAPS[wb.box])).strftime("%Y-%m-%d")


# 段位体系(留存:做题越多段位越高,零成本纯计算)
RANKS = [(0, "萌新上路", "🌱"), (50, "童生", "📖"), (200, "秀才", "✒️"),
         (500, "举人", "🏮"), (1000, "贡士", "🎓"), (2000, "进士", "🏆"),
         (3500, "翰林", "👑"), (5000, "状元", "🐉"), (8000, "上岸在望", "⛵")]


def _rank_of(done: int):
    cur = RANKS[0]
    nxt = None
    for r in RANKS:
        if done >= r[0]:
            cur = r
        elif nxt is None:
            nxt = r
    if nxt is None:
        return {"name": cur[1], "icon": cur[2], "next": None, "need": 0, "pct": 100}
    span = nxt[0] - cur[0]
    return {"name": cur[1], "icon": cur[2], "next": nxt[1],
            "need": nxt[0] - done, "pct": round((done - cur[0]) / span * 100)}


# ============ 认证 ============
def _log_login(username, ip, ok):
    try:
        db = SessionLocal()
        db.add(LoginLog(username=username[:64], ip=ip[:64], ok=1 if ok else 0))
        db.commit()
        db.close()
    except Exception:
        pass


@app.get("/api/auth/config")
def auth_config():
    """注册页需要知道的公开配置(不含敏感信息)。"""
    return {"invite_required": bool(config.INVITE_REQUIRED)}


@app.post("/api/auth/register")
def register(request: Request, username: str = Form(...), password: str = Form(...),
             admin_code: str = Form(""), invite: str = Form("")):
    username = username.strip()
    invite = invite.strip()[:32]
    # 用户名:3-20位,仅限中文/字母/数字/下划线(防注入垃圾字符与超长DoS)
    if not re.fullmatch(r"[\w一-龥]{3,20}", username):
        raise HTTPException(400, "用户名3~20位,仅限中文、字母、数字、下划线")
    # 密码上限72位:PBKDF2 对超长输入是 CPU 炸弹
    if not (6 <= len(password) <= 72):
        raise HTTPException(400, "密码长度6~72位")
    db = SessionLocal()
    if db.query(User).filter(User.username == username).first():
        db.close()
        raise HTTPException(409, "用户名已被注册")
    is_first = db.query(User).count() == 0       # 首位注册者=管理员
    # 邀请码提权:仅在管理员明确配置了 XC_ADMIN_CODE 时才生效(默认关闭)
    code_ok = bool(config.ADMIN_SIGNUP_CODE) and admin_code == config.ADMIN_SIGNUP_CODE
    role = 1 if (is_first or code_ok) else 0
    # 邀请码注册控制:开启后,普通注册必须有效邀请码(同码限人数)
    iv = None
    if config.INVITE_REQUIRED and not is_first and role != 1:
        iv = db.query(InviteCode).filter(InviteCode.code == invite,
                                         InviteCode.enabled == 1).first() if invite else None
        if not iv:
            db.close()
            raise HTTPException(403, "需要有效的邀请码才能注册")
        if iv.used_count >= iv.max_uses:
            db.close()
            raise HTTPException(403, "该邀请码名额已用完")
    u = User(username=username, password_hash=auth.hash_password(password),
             role=role, invite_code=invite if iv else "")
    db.add(u)
    if iv:
        iv.used_count += 1
    db.commit()
    token = auth.make_token(u.id, u.role, u.token_ver or 0)
    vo = _user_vo(u)
    db.close()
    _log_login(username, security.client_ip(request), True)
    return {"token": token, "user": vo}


@app.post("/api/auth/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    username = username.strip()[:64]
    ip = security.client_ip(request)
    key = f"{username}|{ip}"
    left = security.login_locked(key)
    if left:
        raise HTTPException(429, f"失败次数过多,已锁定,{left // 60 + 1} 分钟后再试")
    if len(password) > 72:
        raise HTTPException(400, "密码长度异常")
    db = SessionLocal()
    u = db.query(User).filter(User.username == username).first()
    if not u or not auth.verify_password(password, u.password_hash):
        db.close()
        security.login_fail(key)
        _log_login(username, ip, False)
        raise HTTPException(401, "用户名或密码错误")
    if u.status != 1:
        db.close()
        raise HTTPException(403, "账号已被封禁")
    security.login_ok(key)
    # 单设备登录:版本+1,其它设备的旧令牌全部立即失效
    if config.SINGLE_DEVICE:
        u.token_ver = (u.token_ver or 0) + 1
        db.commit()
    token = auth.make_token(u.id, u.role, u.token_ver or 0)
    vo = _user_vo(u)
    db.close()
    _log_login(username, ip, True)
    return {"token": token, "user": vo}


@app.get("/api/auth/me")
def me(u: User = Depends(auth.current_user)):
    return _user_vo(u)


@app.post("/api/auth/change-password")
def change_password(old_password: str = Form(...), new_password: str = Form(...),
                    u: User = Depends(auth.current_user)):
    """改密码:校验旧密码,改完所有设备(含本机旧令牌)强制重新登录。"""
    if not (6 <= len(new_password) <= 72):
        raise HTTPException(400, "新密码长度6~72位")
    db = SessionLocal()
    user = db.get(User, u.id)
    if not auth.verify_password(old_password, user.password_hash):
        db.close()
        raise HTTPException(401, "当前密码错误")
    user.password_hash = auth.hash_password(new_password)
    user.token_ver = (user.token_ver or 0) + 1   # 吊销全部旧令牌
    db.commit()
    token = auth.make_token(user.id, user.role, user.token_ver)
    db.close()
    return {"ok": True, "token": token}


@app.post("/api/auth/logout-all")
def logout_all(u: User = Depends(auth.current_user)):
    """强制下线所有设备(怀疑令牌泄露时用)。"""
    db = SessionLocal()
    user = db.get(User, u.id)
    user.token_ver = (user.token_ver or 0) + 1
    db.commit()
    db.close()
    return {"ok": True}


# ============ 入库(管理员) ============
async def _save_pdf(file: UploadFile, save: str, max_mb: int):
    """流式保存上传的 PDF:校验魔数(防改后缀的恶意文件) + 大小上限。"""
    head = await file.read(5)
    if head != b"%PDF-":
        raise HTTPException(400, "不是有效的 PDF 文件")
    size = len(head)
    try:
        with open(save, "wb") as f:
            f.write(head)
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > max_mb * 1024 * 1024:
                    raise HTTPException(413, f"文件超过 {max_mb}MB")
                f.write(chunk)
    except HTTPException:
        if os.path.exists(save):
            os.remove(save)
        raise


@app.post("/api/ingest/upload")
async def upload(background: BackgroundTasks, file: UploadFile = File(...),
                 admin: User = Depends(auth.require_admin)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "仅支持 PDF")
    save = os.path.join(config.PDF_DIR, f"{uuid.uuid4().hex}.pdf")
    await _save_pdf(file, save, config.ADMIN_PDF_MAX_MB)
    db = SessionLocal()
    job = IngestionJob(file_name=file.filename, file_path=save, scope="public", status=0)
    db.add(job)
    db.commit()
    jid = job.id
    db.close()
    _audit(admin.id, "上传公共题库PDF", file.filename)
    background.add_task(ingest.run, jid)
    return {"job_id": jid}


@app.get("/api/ingest/job/{job_id}")
def job_status(job_id: int, admin: User = Depends(auth.require_admin)):
    db = SessionLocal()
    job = db.get(IngestionJob, job_id)
    db.close()
    if not job:
        raise HTTPException(404, "任务不存在")
    return {"id": job.id, "file_name": job.file_name, "status": job.status,
            "progress": job.progress, "total": job.total_count,
            "done": job.done_count, "dup": job.dup_count,
            "graphic": job.graphic_count, "missing": job.missing_nums,
            "error": job.error_msg}


# ============ 审核(管理员) ============
@app.get("/api/review/pending")
def review_pending(limit: int = 100, admin: User = Depends(auth.require_admin)):
    db = SessionLocal()
    qs = db.query(Question).filter(Question.status == 0).limit(limit).all()
    out = [_q_to_vo(q, db) for q in qs]
    db.close()
    return {"count": len(out), "items": out}


@app.post("/api/review/confirm")
def review_confirm(question_ids: list[int] = Body(...), approve: bool = True,
                   admin: User = Depends(auth.require_admin)):
    db = SessionLocal()
    n = 0
    for qid in question_ids:
        q = db.get(Question, qid)
        if not q:
            continue
        q.status = 1 if approve else 2
        if not approve:
            vectors.delete(qid)
        n += 1
    db.commit()
    db.close()
    _audit(admin.id, "审核题目" if approve else "删除题目", f"{n}道")
    return {"updated": n}


# ============ 出题/做题(登录用户) ============
@app.get("/api/practice/questions")
def serve(l1: str = "", l2: str = "", kp: str = "", difficulty: int = 0, limit: int = 10,
          exclude_done: bool = True, u: User = Depends(auth.current_user)):
    uid = u.id
    db = SessionLocal()
    qy = db.query(Question).filter(Question.status == 1,
                                   Question.scope.in_(["public", f"user:{uid}"]))
    if l1:
        qy = qy.filter(Question.category_l1 == l1)
    if l2:
        qy = qy.filter(Question.category_l2 == l2)
    if kp:
        qy = qy.filter(Question.knowledge_point == kp)
    if difficulty:
        qy = qy.filter(Question.difficulty == difficulty)
    if exclude_done:
        done_ids = [r.question_id for r in db.query(PracticeRecord.question_id)
                    .filter(PracticeRecord.user_id == uid).all()]
        if done_ids:
            qy = qy.filter(~Question.id.in_(done_ids))
    pool = qy.limit(500).all()
    random.shuffle(pool)
    out = [_q_to_vo(q, db, user_id=uid) for q in pool[:limit]]
    db.close()
    return {"count": len(out), "items": out}


@app.post("/api/practice/submit")
def submit(question_id: int = Form(...), user_answer: str = Form(...),
           time_ms: int = Form(0), u: User = Depends(auth.current_user)):
    uid = u.id
    db = SessionLocal()
    q = db.get(Question, question_id)
    if not q:
        db.close()
        raise HTTPException(404, "题不存在")
    has_ans = bool((q.answer or "").strip())
    correct = (user_answer.strip().upper() == q.answer.strip().upper()) if has_ans else None
    db.add(PracticeRecord(user_id=uid, question_id=question_id, user_answer=user_answer,
                          time_ms=max(0, min(time_ms, 30 * 60 * 1000)),
                          is_correct=1 if correct else (0 if correct is False else -1)))
    _update_wrongbook(db, uid, question_id, correct)
    db.commit()
    ans, exp, origin = q.answer, q.explanation, q.answer_origin
    db.close()
    return {"correct": correct, "has_answer": has_ans, "answer": ans,
            "explanation": exp, "answer_origin": origin}


@app.post("/api/questions/{qid}/ai-answer")
def ai_answer(qid: int, u: User = Depends(auth.current_user)):
    """无答案的题让 AI 生成答案+解析(标 origin=ai,生成一次全员共享)。"""
    db = SessionLocal()
    q = db.get(Question, qid)
    if not q or q.status != 1 or \
            (q.scope != "public" and q.scope != f"user:{u.id}"):
        db.close()
        raise HTTPException(404, "题不存在")
    if (q.answer or "").strip() and q.answer_origin != "ai":
        db.close()
        raise HTTPException(409, "本题已有官方答案")
    if (q.answer or "").strip() and q.answer_origin == "ai":
        ans, exp = q.answer, q.explanation     # 已生成过,直接复用(不重复烧钱)
        db.close()
        return {"answer": ans, "explanation": exp, "origin": "ai", "cached": True}
    material = ""
    if q.material_id:
        mg = db.get(MaterialGroup, q.material_id)
        material = mg.material_text if mg else ""
    content, mid = q.content, q.id
    db.close()
    _ai_guard(u)
    r = ai.solve(content, material)
    if not r["answer"]:
        raise HTTPException(422, "AI 无法确定本题答案(图形题/信息不足),建议上传官方答案")
    db = SessionLocal()
    q = db.get(Question, mid)
    q.answer = r["answer"]
    q.answer_origin = "ai"
    if r["explanation"] and not (q.explanation or "").strip():
        q.explanation = r["explanation"]
    if q.paper_id:
        db.flush()
        p = db.get(Paper, q.paper_id)
        if p:
            p.answer_count = db.query(Question).filter(
                Question.paper_id == q.paper_id, Question.status == 1,
                Question.answer != "").count()
    db.commit()
    exp = q.explanation
    db.close()
    return {"answer": r["answer"], "explanation": exp, "origin": "ai", "cached": False}


@app.get("/api/practice/similar/{question_id}")
def similar(question_id: int, k: int = 5, u: User = Depends(auth.current_user)):
    db = SessionLocal()
    q = db.get(Question, question_id)
    if not q:
        db.close()
        raise HTTPException(404, "题不存在")
    hits = vectors.search(q.topic_summary, ["public", f"user:{u.id}"],
                          k=k, exclude_qid=question_id)
    out = []
    for qid, dist in hits:
        sq = db.get(Question, qid)
        if sq and sq.status == 1:
            vo = _q_to_vo(sq, db, user_id=u.id)
            vo["distance"] = round(dist, 4)
            out.append(vo)
    db.close()
    return {"count": len(out), "items": out}


# ============ 考点树(细分题型进度:题量/已做/正确率,薄弱一眼看出) ============
@app.get("/api/category/tree")
def category_tree(u: User = Depends(auth.current_user)):
    uid = u.id
    db = SessionLocal()
    qrows = db.query(Question.id, Question.category_l1, Question.category_l2,
                     Question.knowledge_point) \
        .filter(Question.status == 1,
                Question.scope.in_(["public", f"user:{uid}"])).all()
    rec_rows = db.query(PracticeRecord.question_id, PracticeRecord.is_correct) \
        .filter(PracticeRecord.user_id == uid).all()
    db.close()
    # 每题的作答情况(去重:做过即 done,judged 取全部判分记录)
    per_q = {}
    for qid, ok in rec_rows:
        s = per_q.setdefault(qid, {"judged": 0, "correct": 0})
        if ok in (0, 1):
            s["judged"] += 1
            s["correct"] += ok

    def _node():
        return {"total": 0, "done": 0, "judged": 0, "correct": 0, "children": {}}

    tree = {}
    for qid, l1, l2, kp in qrows:
        l1, l2, kp = l1 or "其他", l2 or "(未细分)", (kp or "").strip()
        n1 = tree.setdefault(l1, _node())
        n2 = n1["children"].setdefault(l2, _node())
        n3 = n2["children"].setdefault(kp, _node()) if kp else None
        s = per_q.get(qid)
        for n in (n1, n2, n3):
            if n is None:
                continue
            n["total"] += 1
            if s:
                n["done"] += 1
                n["judged"] += s["judged"]
                n["correct"] += s["correct"]

    def _fmt(name, n):
        acc = round(n["correct"] / n["judged"] * 100) if n["judged"] else None
        return {"name": name, "total": n["total"], "done": n["done"],
                "accuracy": acc,
                "weak": acc is not None and n["judged"] >= 5 and acc < 60,
                "children": [_fmt(k, v) for k, v in
                             sorted(n["children"].items(),
                                    key=lambda kv: -kv[1]["total"])]}

    out = [_fmt(k, v) for k, v in sorted(tree.items(), key=lambda kv: -kv[1]["total"])]
    return {"items": out}


# ============ 整卷模考(限时模拟+交卷出分,行测核心是时间管理) ============
@app.post("/api/mock/start")
def mock_start(paper_id: int = Form(...), time_limit: int = Form(120),
               u: User = Depends(auth.current_user)):
    if not (5 <= time_limit <= 180):
        raise HTTPException(400, "限时须在 5~180 分钟")
    db = SessionLocal()
    p = _paper_or_403(db, paper_id, u, need_owner=False)
    qs = db.query(Question).filter(Question.paper_id == paper_id,
                                   Question.status == 1) \
        .order_by(Question.seq_no == 0, Question.seq_no, Question.id).all()
    if not qs:
        db.close()
        raise HTTPException(400, "这套卷没有可考的题")
    m = MockExam(user_id=u.id, paper_id=paper_id,
                 time_limit_min=time_limit, total=len(qs))
    db.add(m)
    db.commit()
    out = [_q_to_vo(q, db, user_id=u.id) for q in qs]   # 不带答案
    mid, title = m.id, p.title
    db.close()
    return {"mock_id": mid, "title": title, "time_limit": time_limit,
            "count": len(out), "items": out}


@app.post("/api/mock/submit")
def mock_submit(payload: dict = Body(...), u: User = Depends(auth.current_user)):
    """交卷:统一判分,生成分模块报告;计入做题记录与错题本。"""
    mid = int(payload.get("mock_id") or 0)
    answers = payload.get("answers") or []       # [{id, ans, time_ms}]
    time_used = int(payload.get("time_used_sec") or 0)
    db = SessionLocal()
    m = db.get(MockExam, mid)
    if not m or m.user_id != u.id:
        db.close()
        raise HTTPException(404, "模考不存在")
    if m.submitted:
        db.close()
        raise HTTPException(409, "该模考已交卷")
    ans_map = {int(a.get("id") or 0): a for a in answers if a.get("id")}
    qs = db.query(Question).filter(Question.paper_id == m.paper_id,
                                   Question.status == 1).all()
    by_mod = {}
    detail, correct_n, answered = [], 0, 0
    for q in qs:
        a = ans_map.get(q.id)
        user_ans = (a.get("ans") or "").strip().upper() if a else ""
        t_ms = max(0, min(int(a.get("time_ms") or 0), 30 * 60 * 1000)) if a else 0
        has_ans = bool((q.answer or "").strip())
        right = q.answer.strip().upper() if has_ans else ""
        ok = (user_ans == right) if (has_ans and user_ans) else None
        if user_ans:
            answered += 1
            db.add(PracticeRecord(user_id=u.id, question_id=q.id,
                                  user_answer=user_ans, time_ms=t_ms,
                                  is_correct=1 if ok else (0 if ok is False else -1)))
            _update_wrongbook(db, u.id, q.id, ok)
        if ok:
            correct_n += 1
        mod = by_mod.setdefault(q.category_l1 or "其他",
                                {"total": 0, "answered": 0, "correct": 0, "ms": 0})
        mod["total"] += 1
        if user_ans:
            mod["answered"] += 1
            mod["ms"] += t_ms
        if ok:
            mod["correct"] += 1
        detail.append({"id": q.id, "seq_no": q.seq_no, "your": user_ans,
                       "answer": right, "correct": ok,
                       "explanation": q.explanation or ""})
    report = [{"name": k, **v,
               "avg_sec": round(v["ms"] / v["answered"] / 1000) if v["answered"] else 0}
              for k, v in by_mod.items()]
    m.submitted = 1
    m.answered = answered
    m.correct = correct_n
    m.time_used_sec = max(0, min(time_used, m.time_limit_min * 60 + 60))
    m.report = json.dumps(report, ensure_ascii=False)
    db.commit()
    total = m.total
    db.close()
    score = round(correct_n / total * 100, 1) if total else 0
    return {"ok": True, "total": total, "answered": answered,
            "correct": correct_n, "score": score,
            "time_used_sec": time_used, "by_module": report, "detail": detail}


@app.get("/api/mock/history")
def mock_history(u: User = Depends(auth.current_user)):
    db = SessionLocal()
    ms = db.query(MockExam).filter(MockExam.user_id == u.id,
                                   MockExam.submitted == 1) \
        .order_by(MockExam.id.desc()).limit(20).all()
    out = []
    for m in ms:
        p = db.get(Paper, m.paper_id)
        out.append({"id": m.id, "paper": p.title if p else f"#{m.paper_id}",
                    "total": m.total, "answered": m.answered, "correct": m.correct,
                    "score": round(m.correct / m.total * 100, 1) if m.total else 0,
                    "time_used_sec": m.time_used_sec,
                    "date": m.created_at.strftime("%m-%d %H:%M") if m.created_at else ""})
    db.close()
    return {"count": len(out), "items": out}


# ============ AI 智能找题 / 对话 / 上传自己的题(核心卖点) ============
def _vec_recommend(db, summary_or_text, uid, k=6, exclude=None):
    hits = vectors.search(summary_or_text, ["public", f"user:{uid}"],
                          k=k, exclude_qid=exclude)
    out = []
    for qid, dist in hits:
        sq = db.get(Question, qid)
        if sq and sq.status == 1:
            vo = _q_to_vo(sq, db, user_id=uid)
            vo["match"] = round(max(0.0, 1 - dist), 3)
            out.append(vo)
    return out


def _mine_first_recommend(db, c, msg, uid, k=6):
    """推荐题目:优先从用户自己上传的私有题库里按题型精确调取,
    再用向量检索补足(私库+公共库)。这是「上传PDF→对话调题」闭环的核心。"""
    mine_scope = f"user:{uid}"
    picked, seen = [], set()
    # 1) 私库按分类直查(l2 优先,其次 l1)——用户问"图形推理"就先给他自己传的图形推理
    qy = db.query(Question).filter(Question.scope == mine_scope, Question.status == 1)
    if c.get("l2"):
        mine_qs = qy.filter(Question.category_l2 == c["l2"]).limit(k).all()
    elif c.get("l1"):
        mine_qs = qy.filter(Question.category_l1 == c["l1"]).limit(k).all()
    else:
        mine_qs = []
    random.shuffle(mine_qs)
    for q in mine_qs[:max(2, k // 2)]:
        vo = _q_to_vo(q, db, user_id=uid)
        vo["match"] = 1.0
        picked.append(vo)
        seen.add(q.id)
    # 2) 向量检索补足(覆盖私库+公共库,自然按相似度排)
    hits = vectors.search(c.get("summary") or msg, ["public", mine_scope], k=k + len(seen))
    for qid, dist in hits:
        if qid in seen or len(picked) >= k:
            continue
        sq = db.get(Question, qid)
        if sq and sq.status == 1:
            vo = _q_to_vo(sq, db, user_id=uid)
            vo["match"] = round(max(0.0, 1 - dist), 3)
            picked.append(vo)
            seen.add(qid)
    return picked


@app.post("/api/ai/ask")
def ai_ask(payload: dict = Body(...), u: User = Depends(auth.current_user)):
    """和 AI 对话 + 按你说的内容/贴的题,自动分析考点并推荐对应的题(动态,非固定)。
    推荐优先调用户自己上传的题(私有题库),不够再从公共库补。"""
    msg = (payload.get("message") or "").strip()[:4000]
    if not msg:
        raise HTTPException(400, "请输入内容")
    _ai_guard(u)
    opstats.record_chat()
    history = payload.get("history") or []
    reply = ai.chat(msg, history)
    analysis, items = {}, []
    try:
        # 省钱三段式:词表秒分类(免费) → 贴题才调 LLM → 闲聊不分析不推荐
        c = ai.quick_classify(msg)
        if not c and len(msg) > 60:
            c = ai.classify(msg)
        if c:
            analysis = {"l1": c["l1"], "l2": c["l2"], "l3": c.get("l3", ""),
                        "kp": c["kp"], "summary": c["summary"]}
            db = SessionLocal()
            items = _mine_first_recommend(db, c, msg, u.id, k=6)
            db.close()
    except Exception:
        pass
    return {"reply": reply, "analysis": analysis, "items": items}


@app.post("/api/mine/add")
def mine_add(content: str = Form(...), u: User = Depends(auth.current_user)):
    """用户上传自己的一道题 → 存入个人私有题库 → 立即返回对应/相似的题。"""
    content = content.strip()[:4000]
    if len(content) < 8:
        raise HTTPException(400, "题目内容太短")
    _ai_guard(u)
    scope = f"user:{u.id}"
    c = ai.classify(content)
    db = SessionLocal()
    q = Question(scope=scope, source="我的上传", category_l1=c["l1"], category_l2=c["l2"],
                 knowledge_point=c["kp"], topic_summary=c["summary"], difficulty=c["diff"],
                 content=textutil.clean_text(content), fingerprint="", status=1)
    db.add(q)
    db.commit()
    qid = q.id
    try:
        vid = vectors.upsert(qid, c["summary"], scope, c["l2"])
        db.get(Question, qid).vector_id = vid
        db.commit()
    except Exception:
        pass
    items = _vec_recommend(db, c["summary"], u.id, k=8, exclude=qid)
    db.close()
    return {"saved_id": qid, "analysis": {"l1": c["l1"], "l2": c["l2"],
            "l3": c.get("l3", ""), "kp": c["kp"]}, "count": len(items), "items": items}


@app.post("/api/export/pdf")
def export_pdf(question_ids: list[int] = Body(...), u: User = Depends(auth.current_user)):
    """把一组题目导出成可下载 PDF(像 Claude artifact:对话→右侧整理成可下载文件)。"""
    db = SessionLocal()
    qs = []
    for qid in question_ids[:100]:
        q = db.get(Question, qid)
        if not q:
            continue
        d = {"seq_no": q.seq_no, "category": q.category_l2 or q.category_l1,
             "content": textutil.clean_text(q.content), "answer": q.answer,
             "explanation": q.explanation, "images": [], "material_text": "",
             "material_images": []}
        for im in db.query(QuestionImage).filter(QuestionImage.question_id == qid).all():
            d["images"].append(os.path.join(config.IMAGE_DIR, im.object_key))
        if q.material_id:
            mg = db.get(MaterialGroup, q.material_id)
            if mg:
                d["material_text"] = textutil.clean_text(mg.material_text)
                d["material_images"] = [os.path.join(config.IMAGE_DIR, k)
                                        for k in mg.image_keys.split("|") if k]
        qs.append(d)
    db.close()
    if not qs:
        raise HTTPException(400, "没有可导出的题")
    pdf = pdfgen.build_pdf(qs, "行测题目集")
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": 'attachment; filename="xingce.pdf"'})


@app.post("/api/mine/upload-pdf")
async def mine_upload_pdf(background: BackgroundTasks, file: UploadFile = File(...),
                          u: User = Depends(auth.current_user)):
    """用户上传整份 PDF → AI 切题/分类/抠图 → 全部进个人私有题库。
    之后在 AI 对话里问某类题,系统会优先从这里调取。"""
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "仅支持 PDF 文件")
    db = SessionLocal()
    paper_count = db.query(Paper).filter(Paper.scope == f"user:{u.id}",
                                         Paper.status == 1).count()
    mine_count = db.query(Question).filter(Question.scope == f"user:{u.id}",
                                           Question.status == 1).count()
    running = db.query(IngestionJob).filter(IngestionJob.user_id == u.id,
                                            IngestionJob.status.in_([0, 1, 2])).count()
    today0 = datetime.combine(date.today(), datetime.min.time())
    today_jobs = db.query(IngestionJob).filter(IngestionJob.user_id == u.id,
                                               IngestionJob.created_at >= today0).count()
    db.close()
    if paper_count >= config.MINE_PAPER_CAP:
        raise HTTPException(402, f"已达套卷上限({config.MINE_PAPER_CAP}套),"
                                 "可在「套卷题库」删除不用的卷后再传")
    if mine_count >= config.MINE_CAP:
        raise HTTPException(402, f"私有题库已达上限({config.MINE_CAP}题),请清理后再传")
    if running:
        raise HTTPException(409, "你有一份 PDF 正在解析中,完成后再传下一份")
    if today_jobs >= config.MINE_PDF_DAILY:
        raise HTTPException(429, f"今天已传 {config.MINE_PDF_DAILY} 份,明天再来吧(解析很烧算力)")
    save = os.path.join(config.PDF_DIR, f"u{u.id}_{uuid.uuid4().hex}.pdf")
    await _save_pdf(file, save, config.MINE_PDF_MAX_MB)
    db = SessionLocal()
    job = IngestionJob(user_id=u.id, file_name=file.filename, file_path=save,
                       scope=f"user:{u.id}", status=0)
    db.add(job)
    db.commit()
    jid = job.id
    db.close()
    background.add_task(ingest.run, jid)
    return {"job_id": jid}


@app.get("/api/mine/job/{job_id}")
def mine_job_status(job_id: int, u: User = Depends(auth.current_user)):
    db = SessionLocal()
    job = db.get(IngestionJob, job_id)
    db.close()
    if not job or job.user_id != u.id:
        raise HTTPException(404, "任务不存在")
    return {"id": job.id, "file_name": job.file_name, "status": job.status,
            "progress": job.progress, "total": job.total_count,
            "done": job.done_count, "dup": job.dup_count,
            "missing": job.missing_nums, "error": job.error_msg}


@app.post("/api/mine/delete")
def mine_delete(question_id: int = Form(...), u: User = Depends(auth.current_user)):
    db = SessionLocal()
    q = db.get(Question, question_id)
    if not q or q.scope != f"user:{u.id}":
        db.close()
        raise HTTPException(404, "题不存在或不属于你")
    q.status = 2
    db.commit()
    db.close()
    vectors.delete(question_id)
    return {"ok": True}


@app.get("/api/mine/list")
def mine_list(u: User = Depends(auth.current_user)):
    db = SessionLocal()
    qs = db.query(Question).filter(Question.scope == f"user:{u.id}",
                                   Question.status == 1).order_by(Question.id.desc()).all()
    out = [_q_to_vo(q, db, with_answer=True, user_id=u.id) for q in qs]
    # 各题型分布(对话调题时给用户感知:他的库里有什么)
    by_cat = {}
    for q in qs:
        key = q.category_l2 or q.category_l1 or "其他"
        by_cat[key] = by_cat.get(key, 0) + 1
    db.close()
    return {"count": len(out), "cap": config.MINE_CAP, "by_category": by_cat, "items": out}


# ============ 套卷题库(题库以 PDF/套卷为单位展示) ============
def _paper_or_403(db, pid: int, u: User, need_owner=True):
    p = db.get(Paper, pid)
    if not p or p.status != 1:
        db.close()
        raise HTTPException(404, "套卷不存在")
    visible = p.scope == "public" or p.scope == f"user:{u.id}"
    if not visible:
        db.close()
        raise HTTPException(404, "套卷不存在")
    if need_owner:
        owns = p.scope == f"user:{u.id}" or (p.scope == "public" and u.role == 1)
        if not owns:
            db.close()
            raise HTTPException(403, "无权操作该套卷")
    return p


@app.get("/api/papers")
def papers_list(u: User = Depends(auth.current_user)):
    """套卷列表(公共卷+我的卷),含做题进度。"""
    db = SessionLocal()
    ps = db.query(Paper).filter(Paper.status == 1,
                                Paper.scope.in_(["public", f"user:{u.id}"])) \
        .order_by(Paper.id.desc()).all()
    # 我在每卷已做的题数(一次聚合查询,避免 N+1)
    done_rows = db.query(Question.paper_id, func.count(func.distinct(Question.id))) \
        .join(PracticeRecord, PracticeRecord.question_id == Question.id) \
        .filter(PracticeRecord.user_id == u.id, Question.paper_id != 0) \
        .group_by(Question.paper_id).all()
    done_map = dict(done_rows)
    out = [{"id": p.id, "title": p.title, "mine": p.scope != "public",
            "question_count": p.question_count, "answer_count": p.answer_count,
            "done": done_map.get(p.id, 0),
            "date": p.created_at.strftime("%Y-%m-%d") if p.created_at else ""}
           for p in ps]
    mine_n = sum(1 for p in ps if p.scope != "public")
    db.close()
    return {"count": len(out), "items": out,
            "mine_count": mine_n, "paper_cap": config.MINE_PAPER_CAP}


@app.get("/api/papers/{pid}/questions")
def paper_questions(pid: int, u: User = Depends(auth.current_user)):
    db = SessionLocal()
    p = _paper_or_403(db, pid, u, need_owner=False)
    # 有题号的按题号排,没题号的(seq_no=0)排最后按入库序
    qs = db.query(Question).filter(Question.paper_id == pid, Question.status == 1) \
        .order_by(Question.seq_no == 0, Question.seq_no, Question.id).all()
    out = [_q_to_vo(q, db, user_id=u.id) for q in qs]
    title, ac = p.title, p.answer_count
    can_edit = p.scope == f"user:{u.id}" or (p.scope == "public" and u.role == 1)
    db.close()
    return {"title": title, "answer_count": ac, "can_edit": can_edit,
            "count": len(out), "items": out}


@app.delete("/api/papers/{pid}")
def paper_delete(pid: int, u: User = Depends(auth.current_user)):
    """删除套卷(本人私有卷;公共卷仅管理员)。题目与向量一并删。"""
    db = SessionLocal()
    p = _paper_or_403(db, pid, u)
    qids = [q.id for q in db.query(Question.id).filter(Question.paper_id == pid).all()]
    db.query(Question).filter(Question.paper_id == pid) \
        .update({Question.status: 2}, synchronize_session=False)
    p.status = 2
    db.commit()
    db.close()
    for qid in qids:
        try:
            vectors.delete(qid)
        except Exception:
            pass
    if u.role == 1:
        _audit(u.id, "删套卷", f"#{pid}")
    return {"ok": True}


def _qnum_of(q: Question) -> int:
    """题目的卷内题号:优先 seq_no,否则从题干开头提取。"""
    if q.seq_no:
        return q.seq_no
    m = re.match(r'\s*(\d{1,3})\s*[\.、．]', q.content or "")
    return int(m.group(1)) if m else 0


@app.post("/api/papers/{pid}/answers")
async def paper_answers(pid: int, text: str = Form(""),
                        file: UploadFile = File(None),
                        u: User = Depends(auth.current_user)):
    """给套卷上传答案:粘贴答案文本或传答案 PDF,AI 解析「题号→答案/解析」自动填回。"""
    db = SessionLocal()
    _paper_or_403(db, pid, u)
    db.close()
    if file is not None and file.filename:
        if not file.filename.lower().endswith(".pdf"):
            raise HTTPException(400, "答案文件仅支持 PDF")
        save = os.path.join(config.PDF_DIR, f"ans_{uuid.uuid4().hex}.pdf")
        await _save_pdf(file, save, config.MINE_PDF_MAX_MB)
        try:
            import fitz
            text = "\n".join(p.get_text() for p in fitz.open(save))
        finally:
            try:
                os.remove(save)
            except OSError:
                pass
    text = (text or "").strip()
    if len(text) < 3:
        raise HTTPException(400, "请粘贴答案内容或上传答案 PDF")
    _ai_guard(u)
    key = ai.parse_answer_key(text[:60000])
    if not key:
        raise HTTPException(422, "没有解析出「题号→答案」,请检查答案格式")
    db = SessionLocal()
    qs = db.query(Question).filter(Question.paper_id == pid,
                                   Question.status == 1).all()
    matched = 0
    for q in qs:
        n = _qnum_of(q)
        it = key.get(n)
        if not it:
            continue
        q.answer = it["answer"]
        q.answer_origin = "official"   # 上传的答案覆盖 AI 生成的
        if it.get("explanation"):
            q.explanation = it["explanation"][:4000]
        matched += 1
    db.flush()   # autoflush=False:先把答案写进事务,下面的统计才数得到
    p = db.get(Paper, pid)
    p.answer_count = db.query(Question).filter(Question.paper_id == pid,
                                               Question.status == 1,
                                               Question.answer != "").count()
    db.commit()
    ac = p.answer_count
    db.close()
    return {"ok": True, "parsed": len(key), "matched": matched, "answer_count": ac}


# ============ 全国考试日历 ============
@app.get("/api/exams")
def exams_list(u: User = Depends(auth.current_user)):
    db = SessionLocal()
    rows = db.query(ExamInfo).all()
    db.close()
    # 全国置顶,其余按省份分组;有具体日期的排前面
    rows.sort(key=lambda e: (e.region != "全国", e.region, e.exam_date or "9999"))
    out = [{"id": e.id, "region": e.region, "exam_type": e.exam_type, "name": e.name,
            "signup_start": e.signup_start, "signup_end": e.signup_end,
            "exam_date": e.exam_date, "announce_url": e.announce_url,
            "note": e.note, "origin": e.origin,
            "updated": e.updated_at.strftime("%Y-%m-%d") if e.updated_at else ""}
           for e in rows]
    return {"count": len(out), "items": out}


@app.post("/api/admin/exams")
def exam_save(eid: int = Form(0), region: str = Form(...), name: str = Form(...),
              exam_type: str = Form("省考"), signup_start: str = Form(""),
              signup_end: str = Form(""), exam_date: str = Form(""),
              announce_url: str = Form(""), note: str = Form(""),
              admin: User = Depends(auth.require_admin)):
    """手动新增/修正一条考试信息。"""
    for d in (signup_start, signup_end, exam_date):
        if d and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
            raise HTTPException(400, "日期格式须为 YYYY-MM-DD")
    db = SessionLocal()
    row = db.get(ExamInfo, eid) if eid else None
    if not row:
        row = ExamInfo(region=region.strip()[:32], name=name.strip()[:128])
        db.add(row)
    row.region, row.name = region.strip()[:32], name.strip()[:128]
    row.exam_type = exam_type.strip()[:32]
    row.signup_start, row.signup_end, row.exam_date = signup_start, signup_end, exam_date
    row.announce_url, row.note = announce_url.strip()[:512], note.strip()[:255]
    row.origin = "手动"
    row.updated_at = datetime.now()
    db.commit()
    db.close()
    _audit(admin.id, "改考试日历", name[:64])
    return {"ok": True}


@app.post("/api/admin/exams/fetch")
def exam_fetch(admin: User = Depends(auth.require_admin)):
    """一键自动获取:抓公开信息源 + AI 提取,合并进考试日历。"""
    r = exams.fetch_and_update()
    _audit(admin.id, "抓取考试日历",
           f"+{r['added']} 改{r['updated']} 错{len(r['errors'])}")
    return r


# ============ 邀请码(管理员) ============
@app.get("/api/admin/invites")
def invites_list(admin: User = Depends(auth.require_admin)):
    db = SessionLocal()
    rows = db.query(InviteCode).order_by(InviteCode.id.desc()).all()
    out = [{"id": r.id, "code": r.code, "max_uses": r.max_uses,
            "used_count": r.used_count, "enabled": r.enabled, "note": r.note,
            "date": r.created_at.strftime("%Y-%m-%d") if r.created_at else ""}
           for r in rows]
    db.close()
    return {"items": out, "invite_required": bool(config.INVITE_REQUIRED)}


@app.post("/api/admin/invites")
def invite_create(code: str = Form(""), max_uses: int = Form(10),
                  note: str = Form(""), admin: User = Depends(auth.require_admin)):
    """生成邀请码。code 留空自动生成;max_uses 即该码可注册人数。"""
    code = code.strip()[:32]
    if code and not re.fullmatch(r"[A-Za-z0-9_-]{4,32}", code):
        raise HTTPException(400, "邀请码4~32位,仅限字母数字-_")
    if not code:
        import secrets
        code = secrets.token_hex(4).upper()
    if not (1 <= max_uses <= 10000):
        raise HTTPException(400, "人数限制须在 1~10000")
    db = SessionLocal()
    if db.query(InviteCode).filter(InviteCode.code == code).first():
        db.close()
        raise HTTPException(409, "该邀请码已存在")
    db.add(InviteCode(code=code, max_uses=max_uses, note=note.strip()[:128],
                      created_by=admin.id))
    db.commit()
    db.close()
    _audit(admin.id, "生成邀请码", f"{code}(限{max_uses}人)")
    return {"ok": True, "code": code}


@app.post("/api/admin/invites/{iid}/toggle")
def invite_toggle(iid: int, admin: User = Depends(auth.require_admin)):
    db = SessionLocal()
    r = db.get(InviteCode, iid)
    if not r:
        db.close()
        raise HTTPException(404, "邀请码不存在")
    r.enabled = 0 if r.enabled else 1
    en = r.enabled
    db.commit()
    db.close()
    _audit(admin.id, "启停邀请码", f"#{iid} -> {en}")
    return {"ok": True, "enabled": en}


@app.delete("/api/admin/invites/{iid}")
def invite_delete(iid: int, admin: User = Depends(auth.require_admin)):
    db = SessionLocal()
    r = db.get(InviteCode, iid)
    if r:
        db.delete(r)
        db.commit()
    db.close()
    _audit(admin.id, "删邀请码", f"#{iid}")
    return {"ok": True}


# ============ 错题本/收藏(登录用户) ============
@app.get("/api/wrongbook")
def wrongbook(due: int = 0, u: User = Depends(auth.current_user)):
    """错题本。due=1 只看今日到期待复习的(间隔重复)。"""
    db = SessionLocal()
    qy = db.query(WrongBook).filter(WrongBook.user_id == u.id,
                                    WrongBook.mastered == 0)
    if due:
        today = date.today().strftime("%Y-%m-%d")
        qy = qy.filter(WrongBook.next_review != "", WrongBook.next_review <= today)
    out = []
    for wb in qy.all():
        q = db.get(Question, wb.question_id)
        if q and q.status == 1:
            vo = _q_to_vo(q, db, user_id=u.id)
            vo["wrong_count"] = wb.wrong_count
            vo["next_review"] = wb.next_review
            out.append(vo)
    db.close()
    return {"count": len(out), "items": out}


@app.get("/api/practice/weak")
def weak_set(limit: int = 10, u: User = Depends(auth.current_user)):
    """弱项特训:找正确率最低的题型(判过分≥5题),自动组一卷没做过的题。"""
    uid = u.id
    db = SessionLocal()
    rows = db.query(PracticeRecord.question_id, PracticeRecord.is_correct) \
        .filter(PracticeRecord.user_id == uid,
                PracticeRecord.is_correct.in_([0, 1])).all()
    qcat = dict(db.query(Question.id, Question.category_l2).filter(
        Question.id.in_([r[0] for r in rows]) if rows else False).all()) if rows else {}
    acc = {}
    for qid, ok in rows:
        l2 = qcat.get(qid) or ""
        if not l2:
            continue
        s = acc.setdefault(l2, [0, 0])
        s[0] += 1
        s[1] += ok
    weak = sorted(((l2, round(c / n * 100)) for l2, (n, c) in acc.items()
                   if n >= 5 and c / n < 0.7), key=lambda x: x[1])[:3]
    if not weak:
        db.close()
        return {"count": 0, "focus": [], "items": [],
                "hint": "先多做些题(每个题型至少5题),系统才能定位你的弱项"}
    done_ids = {r[0] for r in rows}
    pool = db.query(Question).filter(
        Question.status == 1,
        Question.scope.in_(["public", f"user:{uid}"]),
        Question.category_l2.in_([w[0] for w in weak]),
        ~Question.id.in_(done_ids)).limit(300).all()
    if len(pool) < limit:   # 没做过的不够,做过的也拿来重练
        pool += db.query(Question).filter(
            Question.status == 1,
            Question.scope.in_(["public", f"user:{uid}"]),
            Question.category_l2.in_([w[0] for w in weak]),
            Question.id.in_(done_ids)).limit(100).all()
    random.shuffle(pool)
    out = [_q_to_vo(q, db, user_id=uid) for q in pool[:limit]]
    db.close()
    return {"count": len(out), "items": out,
            "focus": [{"name": w[0], "accuracy": w[1]} for w in weak]}


@app.post("/api/wrongbook/master")
def master(question_id: int = Form(...), u: User = Depends(auth.current_user)):
    db = SessionLocal()
    wb = db.query(WrongBook).filter(WrongBook.user_id == u.id,
                                    WrongBook.question_id == question_id).first()
    if wb:
        wb.mastered = 1
        db.commit()
    db.close()
    return {"ok": True}


@app.post("/api/favorite/toggle")
def fav_toggle(question_id: int = Form(...), u: User = Depends(auth.current_user)):
    db = SessionLocal()
    f = db.query(Favorite).filter(Favorite.user_id == u.id,
                                  Favorite.question_id == question_id).first()
    if f:
        db.delete(f)
        faved = False
    else:
        db.add(Favorite(user_id=u.id, question_id=question_id))
        faved = True
    db.commit()
    db.close()
    return {"favorited": faved}


@app.get("/api/favorite/list")
def fav_list(u: User = Depends(auth.current_user)):
    db = SessionLocal()
    favs = db.query(Favorite).filter(Favorite.user_id == u.id) \
        .order_by(Favorite.id.desc()).all()
    out = [_q_to_vo(db.get(Question, f.question_id), db, with_answer=True, user_id=u.id)
           for f in favs if db.get(Question, f.question_id)]
    db.close()
    return {"count": len(out), "items": out}


# ============ 仪表盘 ============
def _streak(dates_set):
    s, d = 0, date.today()
    if d not in dates_set:
        d = d - timedelta(days=1)
    while d in dates_set:
        s += 1
        d = d - timedelta(days=1)
    return s


@app.get("/api/stats")
def stats(u: User = Depends(auth.current_user)):
    uid = u.id
    db = SessionLocal()
    total_q = db.query(Question).filter(Question.status == 1).count()
    pending = db.query(Question).filter(Question.status == 0).count()
    recs = db.query(PracticeRecord).filter(PracticeRecord.user_id == uid).all()
    judged = [r for r in recs if r.is_correct in (0, 1)]
    correct = sum(1 for r in judged if r.is_correct == 1)
    wrong = db.query(WrongBook).filter(WrongBook.user_id == uid,
                                       WrongBook.mastered == 0).count()
    due_review = db.query(WrongBook).filter(
        WrongBook.user_id == uid, WrongBook.mastered == 0,
        WrongBook.next_review != "",
        WrongBook.next_review <= date.today().strftime("%Y-%m-%d")).count()
    fav = db.query(Favorite).filter(Favorite.user_id == uid).count()
    day_set = {r.created_at.date() for r in recs if r.created_at}
    today = date.today()
    today_count = sum(1 for r in recs if r.created_at and r.created_at.date() == today)
    trend = [{"date": (today - timedelta(days=i)).strftime("%m-%d"),
              "count": sum(1 for r in recs if r.created_at
                           and r.created_at.date() == today - timedelta(days=i))}
             for i in range(6, -1, -1)]
    by_cat = {}
    for r in recs:
        q = db.get(Question, r.question_id)
        if not q:
            continue
        c = by_cat.setdefault(q.category_l1 or "其他",
                              {"done": 0, "correct": 0, "judged": 0, "ms": 0, "timed": 0})
        c["done"] += 1
        if r.is_correct in (0, 1):
            c["judged"] += 1
            c["correct"] += 1 if r.is_correct == 1 else 0
        if r.time_ms:
            c["timed"] += 1
            c["ms"] += r.time_ms
    cats = [{"name": k, "done": v["done"],
             "accuracy": round(v["correct"] / v["judged"] * 100) if v["judged"] else None,
             "avg_sec": round(v["ms"] / v["timed"] / 1000) if v["timed"] else None}
            for k, v in by_cat.items()]
    try:
        days_left = (datetime.strptime(config.EXAM_DATE, "%Y-%m-%d").date() - today).days
    except Exception:
        days_left = None
    db.close()
    return {"total_q": total_q, "pending": pending, "done": len(recs), "correct": correct,
            "accuracy": round(correct / len(judged) * 100, 1) if judged else None,
            "wrong": wrong, "due_review": due_review,
            "favorite": fav, "streak": _streak(day_set),
            "today_count": today_count, "active_days": len(day_set),
            "trend": trend, "by_category": cats,
            "exam_name": config.EXAM_NAME, "exam_days_left": days_left,
            "daily_goal": config.DAILY_GOAL, "rank": _rank_of(len(recs)),
            "online": opstats.online_count()}


# ============ 运营统计 ============
@app.get("/api/online")
def online(u: User = Depends(auth.current_user)):
    """在线人数(5分钟内活跃)。所有登录用户可见,也是前端的心跳接口。"""
    return {"online": opstats.online_count()}


@app.post("/api/track/visit")
def track_visit(u: User = Depends(auth.optional_user)):
    """页面打开埋点:落地页/应用启动各调一次。未登录只记 PV。"""
    opstats.record_visit(u.id if u else 0)
    return {"ok": True}


@app.get("/api/admin/settings")
def get_settings(admin: User = Depends(auth.require_admin)):
    """当前运行配置(敏感值只露尾4位)。来源:管理面板改过的存库,否则用 .env。"""
    out = []
    for skey, (attr, secret) in RUNTIME_SETTINGS.items():
        val = getattr(config, attr, "")
        val = str(val).lower() if isinstance(val, bool) else (val or "")
        shown = (("****" + val[-4:]) if len(val) > 4 else ("已设置" if val else "")) \
            if secret else val
        out.append({"key": skey, "value": shown, "set": bool(val), "secret": secret})
    return {"items": out}


@app.post("/api/admin/settings")
def save_setting(skey: str = Form(...), sval: str = Form(...),
                 admin: User = Depends(auth.require_admin)):
    """改配置(白名单内):立即生效 + 存库(重启不丢) + 审计留痕。"""
    skey = skey.strip()
    sval = sval.strip()
    if skey not in RUNTIME_SETTINGS:
        raise HTTPException(400, "不允许修改该配置项")
    if len(sval) > 500:
        raise HTTPException(400, "值过长")
    db = SessionLocal()
    row = db.query(Setting).filter(Setting.skey == skey).first()
    if row:
        row.sval = sval
        row.updated_at = datetime.now()
    else:
        db.add(Setting(skey=skey, sval=sval))
    db.commit()
    db.close()
    _apply_setting(skey, sval)
    _audit(admin.id, "改配置", skey)     # 只记键名,不记密钥内容
    return {"ok": True}


# ============ AI 渠道管理(多 API 自动故障切换) ============
def _mask_key(k: str) -> str:
    return ("****" + k[-4:]) if len(k or "") > 4 else ("已设置" if k else "")


@app.get("/api/admin/channels")
def list_channels(admin: User = Depends(auth.require_admin)):
    """渠道列表(key 只露尾4位)。按优先级排序,小的先用。"""
    db = SessionLocal()
    rows = db.query(AiChannel).order_by(AiChannel.priority, AiChannel.id).all()
    out = [{"id": r.id, "name": r.name, "base_url": r.base_url, "model": r.model,
            "api_key": _mask_key(r.api_key), "enabled": r.enabled,
            "priority": r.priority, "supports_vision": r.supports_vision,
            "fail_count": r.fail_count,
            "last_error": r.last_error} for r in rows]
    db.close()
    return {"items": out}


@app.post("/api/admin/channels")
def save_channel(cid: int = Form(0), name: str = Form(...),
                 base_url: str = Form(...), model: str = Form(...),
                 api_key: str = Form(""), priority: int = Form(10),
                 supports_vision: int = Form(0),
                 admin: User = Depends(auth.require_admin)):
    """新增/编辑渠道。编辑时 api_key 留空表示不改。"""
    name, base_url, model = name.strip(), base_url.strip().rstrip("/"), model.strip()
    api_key = api_key.strip()
    if not (name and base_url and model):
        raise HTTPException(400, "名称/地址/模型不能为空")
    if not base_url.startswith(("http://", "https://")):
        raise HTTPException(400, "服务地址须以 http(s):// 开头")
    db = SessionLocal()
    if cid:
        row = db.get(AiChannel, cid)
        if not row:
            db.close()
            raise HTTPException(404, "渠道不存在")
        row.name, row.base_url, row.model, row.priority = name, base_url, model, priority
        row.supports_vision = 1 if supports_vision else 0
        if api_key:
            row.api_key = api_key
    else:
        if not api_key:
            db.close()
            raise HTTPException(400, "新渠道必须填 API Key")
        row = AiChannel(name=name, base_url=base_url, model=model,
                        api_key=api_key, priority=priority,
                        supports_vision=1 if supports_vision else 0)
        db.add(row)
    db.commit()
    rid = row.id
    db.close()
    ai.reload_channels()
    _audit(admin.id, "改AI渠道", f"#{rid} {name}")   # 只记名称,不记密钥
    return {"ok": True, "id": rid}


@app.post("/api/admin/channels/{cid}/toggle")
def toggle_channel(cid: int, admin: User = Depends(auth.require_admin)):
    db = SessionLocal()
    row = db.get(AiChannel, cid)
    if not row:
        db.close()
        raise HTTPException(404, "渠道不存在")
    row.enabled = 0 if row.enabled else 1
    en = row.enabled
    db.commit()
    db.close()
    ai.reload_channels()
    _audit(admin.id, "启停AI渠道", f"#{cid} -> {en}")
    return {"ok": True, "enabled": en}


@app.delete("/api/admin/channels/{cid}")
def delete_channel(cid: int, admin: User = Depends(auth.require_admin)):
    db = SessionLocal()
    row = db.get(AiChannel, cid)
    if row:
        db.delete(row)
        db.commit()
    db.close()
    ai.reload_channels()
    _audit(admin.id, "删AI渠道", f"#{cid}")
    return {"ok": True}


@app.post("/api/admin/channels/{cid}/test")
def test_channel(cid: int, admin: User = Depends(auth.require_admin)):
    """连通性测试:真实调一次最小请求,顺便清零失败计数。"""
    db = SessionLocal()
    row = db.get(AiChannel, cid)
    if not row:
        db.close()
        raise HTTPException(404, "渠道不存在")
    ok, msg = ai.test_channel(row.base_url, row.api_key, row.model)
    if ok:
        row.fail_count = 0
        row.last_error = ""
    else:
        row.last_error = msg
    db.commit()
    db.close()
    return {"ok": ok, "msg": msg}


@app.get("/api/admin/metrics")
def admin_metrics(admin: User = Depends(auth.require_admin)):
    """运营面板(仅管理员):人数/人次/对话/token/成本/题库规模 + 近14天趋势。"""
    db = SessionLocal()
    today = date.today().strftime("%Y-%m-%d")
    t = db.query(StatDaily).filter(StatDaily.day == today).first()
    tot = db.query(func.coalesce(func.sum(StatDaily.pv), 0),
                   func.coalesce(func.sum(StatDaily.chat_count), 0),
                   func.coalesce(func.sum(StatDaily.tokens_in), 0),
                   func.coalesce(func.sum(StatDaily.tokens_out), 0)).first()
    users = db.query(User).count()
    new_today = db.query(User).filter(
        User.created_at >= datetime.combine(date.today(), datetime.min.time())).count()
    uv_today = db.query(VisitDay).filter(VisitDay.day == today).count()
    q_pub = db.query(Question).filter(Question.status == 1,
                                      Question.scope == "public").count()
    q_mine = db.query(Question).filter(Question.status == 1,
                                       Question.scope != "public").count()
    practice_total = db.query(PracticeRecord).count()
    trend = [{"day": r.day[5:], "pv": r.pv, "chat": r.chat_count,
              "tokens": r.tokens_in + r.tokens_out}
             for r in db.query(StatDaily).order_by(StatDaily.day.desc()).limit(14).all()][::-1]
    # 安全监控:今日登录失败 + 失败最多的IP(发现爆破一眼就看到)
    day0 = datetime.combine(date.today(), datetime.min.time())
    fail_today = db.query(LoginLog).filter(LoginLog.ok == 0,
                                           LoginLog.created_at >= day0).count()
    bad_ips = db.query(LoginLog.ip, func.count(LoginLog.id)) \
        .filter(LoginLog.ok == 0, LoginLog.created_at >= day0) \
        .group_by(LoginLog.ip).order_by(func.count(LoginLog.id).desc()).limit(5).all()
    db.close()

    def _cost(tin, tout):
        return round((tin * config.PRICE_IN_PER_M + tout * config.PRICE_OUT_PER_M) / 1e6, 2)

    return {
        "online": opstats.online_count(),
        "today": {"pv": t.pv if t else 0, "uv": uv_today,
                  "chat": t.chat_count if t else 0,
                  "tokens_in": t.tokens_in if t else 0,
                  "tokens_out": t.tokens_out if t else 0,
                  "cost": _cost(t.tokens_in if t else 0, t.tokens_out if t else 0),
                  "new_users": new_today},
        "total": {"pv": tot[0], "chat": tot[1], "tokens_in": tot[2],
                  "tokens_out": tot[3], "cost": _cost(tot[2], tot[3]),
                  "users": users, "practice": practice_total},
        "bank": {"public": q_pub, "mine": q_mine},
        "security": {"login_fail_today": fail_today,
                     "locked_now": security.locked_count(),
                     "bad_ips": [{"ip": ip, "fails": n} for ip, n in bad_ips]},
        "ops": security.integrity_status(),
        "trend": trend,
    }


# ============ 资讯/公告 ============
@app.get("/api/news")
def news_list(limit: int = 20):
    db = SessionLocal()
    items = db.query(News).order_by(News.pinned.desc(), News.id.desc()).limit(limit).all()
    out = [{"id": n.id, "title": n.title, "summary": n.summary, "content": n.content,
            "category": n.category, "url": n.url, "pinned": n.pinned,
            "date": n.created_at.strftime("%Y-%m-%d") if n.created_at else ""}
           for n in items]
    db.close()
    return {"count": len(out), "items": out}


@app.post("/api/news")
def news_add(title: str = Form(...), summary: str = Form(""), content: str = Form(""),
             category: str = Form("资讯"), url: str = Form(""), pinned: int = Form(0),
             admin: User = Depends(auth.require_admin)):
    db = SessionLocal()
    n = News(title=title, summary=summary, content=content, category=category,
             url=url, pinned=pinned)
    db.add(n)
    db.commit()
    nid = n.id
    db.close()
    _audit(admin.id, "发布资讯", title)
    return {"id": nid}


@app.delete("/api/news/{nid}")
def news_del(nid: int, admin: User = Depends(auth.require_admin)):
    db = SessionLocal()
    n = db.get(News, nid)
    if n:
        db.delete(n)
        db.commit()
    db.close()
    return {"ok": True}


# ============ 健康检查(部署用) ============
@app.get("/api/health")
def health():
    return {"ok": True, "secret_default": config.SECRET_IS_DEFAULT}


# 前端页面(SPA)
if os.path.isdir(os.path.join(config.BASE_DIR, "static")):
    app.mount("/", StaticFiles(directory=os.path.join(config.BASE_DIR, "static"),
                               html=True), name="static")
