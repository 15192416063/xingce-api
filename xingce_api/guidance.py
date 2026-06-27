# -*- coding: utf-8 -*-
"""AI 引导式思维框架 · 业务层。
题目进来 → 检索方法论(Chroma 按模块硬过滤)→ 注入守护层(system 角色)→ 带约束生成
「一个高手看到这题会怎么想」的【分步】思维拆解(steps[],不直接给答案)→ 轻量越权校验 + 落日志。
用户「没懂」时走重讲分支 explain_stuck(针对具体卡点换说法讲透),并把卡点结构化沉淀。

分层约定:本模块不依赖 FastAPI,只调数据层(取题/取画像/向量检索)与 ai 出口,
        HarmonyOS / 小程序端可直接复用,无需重写。
"""
import os
import re
import json
import logging

import config
import ai
import method_kb
import method_vectors
import stuck_presets
from db import (SessionLocal, Question, QuestionImage, MaterialGroup, UserMemory,
                GuidanceLog, GuidanceCache, StuckRecord)

_logger = logging.getLogger("xc")

_METHOD_CHAR_BUDGET = 2400   # ≈1500 tokens(中文),方法论部分上限;过长截断而非全塞
_RETRIEVE_K = 3

# 题目块尾部再钉一句(贴着题面,模型最容易遵守)。本链路刻意【不】把官方答案喂给模型,
# 从源头降低泄题风险——守护层只在 system 角色。
_NO_LEAK_REMINDER = ("\n\n(请只示范【高手会怎么想】的分步思路与排查标准,"
                     "把「到底选哪个」留给我自己判断,不要公布答案或点破最终选项。)")

# 疑似直接给答案(守护层被突破)的特征。MVP 阶段命中只标记+落日志,不阻断不重试。
_LEAK_PATTERNS = [
    re.compile(r"正确答案"),
    re.compile(r"答案\s*[是为应：:]"),
    re.compile(r"(应|故|因此|所以|可见)\s*选\s*[ABCD]"),
    re.compile(r"(本题|此题|该题)\s*[^。\n]{0,6}选\s*[ABCD]"),
    re.compile(r"选\s*[ABCD]\s*项"),
    re.compile(r"答案\s*为?\s*[ABCD](?![a-zA-Z])"),
    re.compile(r"只有\s*[ABCD][^。\n]{0,4}(符合|满足|正确|对|可以|是它)"),
    re.compile(r"[ABCD]\s*选项[^。\n]{0,6}(符合|正确|满足|就是)"),
]


# ---------- 数据层:取题 / 取画像 / 落日志 ----------
def _question_images(db, q):
    """收集本题相关图的【绝对路径】:资料分析材料组图表 + 本题自身图(图形题/选项图)。
    供视觉模型读图——图形推理这类纯图题不读图,引导必然和题目脱节。文件不存在的跳过。"""
    rels = []
    if q.material_id:
        mg = db.get(MaterialGroup, q.material_id)
        if mg and mg.image_keys:
            rels += [k for k in mg.image_keys.split("|") if k]
    for img in db.query(QuestionImage).filter(QuestionImage.question_id == q.id).all():
        if img.object_key:
            rels.append(img.object_key)
    out = []
    for rel in rels:
        p = os.path.join(config.IMAGE_DIR, rel)
        if os.path.exists(p) and p not in out:
            out.append(p)
    return out


def _load_question(question_id):
    """从 SQLite 取题:返回 {id, module, question_type, content, material, answer, images} 或 None。
    answer=题库官方答案(仅作隐藏锚点保证思路正确,绝不展示);images=题面图绝对路径。"""
    db = SessionLocal()
    try:
        q = db.get(Question, int(question_id))
        if not q or q.status == 2:
            return None
        material = ""
        if q.material_id:
            mg = db.get(MaterialGroup, q.material_id)
            material = mg.material_text if mg else ""
        return {"id": q.id, "module": q.category_l1 or "",
                "question_type": q.category_l2 or "",
                "content": q.content or "", "material": material,
                "answer": (q.answer or "").strip(),
                "images": _question_images(db, q)}
    finally:
        db.close()


def _load_profile(user_id):
    """读取用户能力画像(UserMemory);无 user_id 或无画像 → 空串(不影响主链路)。"""
    if not user_id:
        return ""
    db = SessionLocal()
    try:
        m = db.query(UserMemory).filter(UserMemory.user_id == int(user_id)).first()
        return (m.content if m else "") or ""
    finally:
        db.close()


def _log(question_id, user_id, text, method_ids, guard_triggered):
    """每次生成记一条流水(失败不影响主流程)。"""
    try:
        db = SessionLocal()
        db.add(GuidanceLog(question_id=int(question_id), user_id=int(user_id or 0),
                           retrieved_methods=",".join(method_ids)[:255],
                           guidance_text=(text or "")[:4000],
                           guard_triggered=1 if guard_triggered else 0))
        db.commit()
        db.close()
    except Exception:
        _logger.exception("guidance log failed")


def record_stuck(user_id, question_id, point_id, point_label, step_index,
                 source, raw_text=""):
    """卡点沉淀(本期只写不读):写一条 stuck_records。module/question_type 自动补全。"""
    q = _load_question(question_id)
    try:
        db = SessionLocal()
        db.add(StuckRecord(
            user_id=int(user_id or 0), question_id=int(question_id),
            module=(q["module"] if q else "")[:32],
            question_type=(q["question_type"] if q else "")[:64],
            point_id=(point_id or "")[:64], point_label=(point_label or "")[:128],
            step_index=int(step_index or 0), source=(source or "")[:16],
            raw_text=(raw_text or "")[:2000]))
        db.commit()
        db.close()
    except Exception:
        _logger.exception("stuck record failed")


# ---------- 业务子步骤(各自独立,便于单测/复用) ----------
def _label_of(hit):
    """方法论可读名:优先 metadata.label;旧向量没存 label 时,从注入文本首行【…】兜底解析。"""
    lab = (hit.get("label") or "").strip()
    if lab:
        return lab
    m = re.match(r"\s*【([^】]+)】", hit.get("text", "") or "")
    return m.group(1) if m else (hit.get("id") or "")


def retrieve_methods(module, question_type, content):
    """第一步:检索方法论。Chroma 按 module 硬过滤 + 向量 top-3。
    向量库不可用(没配 embed key 等)时退回 method_kb 关键词路由,保证主链路不空转。
    返回 [{id, text, label}]。"""
    hits = method_vectors.search(content, module, question_type, k=_RETRIEVE_K)
    methods = [{"id": h["id"], "text": h["text"], "label": _label_of(h)}
               for h in hits if h.get("text")]
    if not methods:
        kw = method_kb.method_context(module, question_type, content)
        if kw:
            methods = [{"id": "kw:" + (question_type or module or "fallback"),
                        "text": kw, "label": (question_type or module or "方法论")}]
    return methods


def _budget_methods(methods, budget=_METHOD_CHAR_BUDGET):
    """给方法论部分设上限:超预算的条目截断而非全塞,防止稀释模型注意力。"""
    out, used = [], 0
    for m in methods:
        if used >= budget:
            break
        text = m["text"]
        room = budget - used
        if len(text) > room:
            text = text[:room] + "…(略)"
        out.append({"id": m["id"], "text": text, "label": m.get("label", "")})
        used += len(text)
    return out


def _methods_block(methods):
    return ("【本题可用方法论(严格据此组织思路,不自创解法)】\n" +
            "\n\n".join(f"[{m['id']} = {m.get('label', '')}]\n{m['text']}"
                        for m in methods))


def _question_block(question):
    qb = "【题目】\n" + (question.get("content") or "")
    if question.get("images"):
        qb += "\n(本题附有图片,请先看懂图中内容再拆解思路)"
    if question.get("material"):
        qb += "\n\n【材料】\n" + question["material"][:1500]
    ans = (question.get("answer") or "").strip()
    if ans:
        qb += (
            f"\n\n【标准答案={ans}(题库官方答案,铁定正确;仅你内部参照)】\n"
            f"硬性要求:你的判型、规律、每一步排查标准都必须跟「正确答案是 {ans}」自洽。"
            f"若你读图/推规律得到的指向不是 {ans},那一定是你这步想错了或图看错了"
            f"(图形/立体题尤其容易看错)——请换条规律、重新读图,直到你的方法能让 {ans} 成立;"
            f"绝不允许把学生导向除 {ans} 以外的任何选项。"
            f"但全程【不许写出、点破或暗示 {ans}】,也不许对任何选项下「符合/排除/正确」的结论,"
            f"把「用这个标准逐项核对、锁定答案」这最后一步留给学生自己做。")
    return qb


# ---------- 数据层:引导缓存(按题共享,全员复用) ----------
def _cache_get(qid):
    db = SessionLocal()
    try:
        row = db.query(GuidanceCache).filter(GuidanceCache.question_id == int(qid)).first()
        if not row or not row.steps_json:
            return None
        try:
            steps = json.loads(row.steps_json)
        except Exception:
            return None
        return {"steps": steps,
                "method_ids": row.method_ids.split(",") if row.method_ids else [],
                "guard_triggered": bool(row.guard_triggered)}
    finally:
        db.close()


def is_cached(question_id):
    """本题是否已有缓存引导(供接入层决定是否计入每日 AI 配额:命中缓存不该扣额)。"""
    try:
        return _cache_get(question_id) is not None
    except Exception:
        return False


def _cache_put(qid, steps, method_ids, guard_triggered):
    try:
        db = SessionLocal()
        row = db.query(GuidanceCache).filter(GuidanceCache.question_id == int(qid)).first()
        payload = json.dumps(steps, ensure_ascii=False)
        if row:
            row.steps_json = payload
            row.method_ids = ",".join(method_ids)[:255]
            row.guard_triggered = 1 if guard_triggered else 0
        else:
            db.add(GuidanceCache(question_id=int(qid), steps_json=payload,
                                 method_ids=",".join(method_ids)[:255],
                                 guard_triggered=1 if guard_triggered else 0))
        db.commit()
        db.close()
    except Exception:
        _logger.exception("guidance cache put failed")


# 分步输出格式(JSON),拼在题目块之后。让模型把「高手怎么想」拆成递进步骤。
_STEPS_FORMAT = (
    "\n\n————\n请把【高手会怎么想】拆成 3~5 个**循序渐进**的步骤,"
    "**只输出下面这个 JSON**,不要任何其它文字:\n"
    '{"steps":[{"tag":"≤5字环节标签(如 看变化/定公式/排干扰)",'
    '"title":"这一步的小标题","body":"这一步的引导文字:讲清这步看什么、想什么、'
    '按什么标准判断;Markdown 分点,公式用 LaTeX(行内 $...$)","point_id":"该步对应的方法论ID"}]}\n'
    "要求:\n"
    "- point_id 从【可选方法论ID】里挑**最贴切的一个**,原样填写;\n"
    "- 步骤要递进:判型 → 下手点 → 调用方法 → 逐项排查标准 → 把结论交还学生;\n"
    "- 题目附图时(图形推理/资料分析图表)**必须先看懂图再拆**,讲的内容要紧扣图里实际看到的特征,"
    "不要泛泛套话、不要和本题无关;\n"
    "- 「排查」这一步**只讲【按什么标准、怎么逐项验证】**,绝不要替学生数出/算出每个选项的具体数值,"
    "更不要对 A/B/C/D 任何一项下「符合/排除/就是它」的结论——把动手核对和最终锁定留给学生;\n"
    "- 全程**绝不公布答案、不点破选项**,不要写「答案是X」「选X」「只有X符合」「问号处应为…所以选…」之类。\n"
    "【可选方法论ID】\n{idlist}"
)


# 图形推理纯图题:AI 读图本就不稳,硬让它逐项数图常常数错、反把学生导向错选项。
# 所以图推只教【判型 + 该数/比什么 + 怎么数】,绝不替学生算每个选项、不下任一结论。
_TX_EXTRA = (
    "\n\n⚠️本题是【图形推理】纯图题,AI 读图极易出错。所以你只示范判型与方法:"
    "讲清该往哪个维度想(数量/位置/样式/属性/空间)、要数什么/比什么、怎么数才不漏不重;"
    "绝对不要去数出或断言每个选项的具体数值,绝对不要说哪个选项符合/排除/就是它——"
    "把「具体数每个选项、逐项核对、锁定答案」整个留给学生。最后一步只交还方法,不给任何结论。")


def _is_tuxing(question):
    return "图形" in (question.get("question_type") or "") or \
           "图形" in (question.get("module") or "")


def assemble_steps(system_prompt, methods, question):
    """组装【分步生成】上下文:守护层(system)→ 方法论 + 题目 + JSON 格式要求(user)。
    引导按题共享缓存,故不注入个人画像(避免把某个人的学情写进全员复用的引导)。
    返回 (system_text, user_blocks)。"""
    blocks = []
    if methods:
        blocks.append(_methods_block(methods))
    idlist = "\n".join(f"- {m['id']} = {m.get('label', '')}" for m in methods) or "(无)"
    extra = _TX_EXTRA if _is_tuxing(question) else ""
    blocks.append(_question_block(question) + _NO_LEAK_REMINDER +
                  _STEPS_FORMAT.replace("{idlist}", idlist) + extra)
    return system_prompt, blocks


def _balanced_objects(s):
    """从一段(可能被截断的)文本里,按花括号配平扫出所有完整的 {...} 对象,
    字符串内的 { } 和转义不计数。能从「半截 JSON」里救回已写完的那几个 step 对象。"""
    objs, depth, start, instr, esc = [], 0, -1, False, False
    for i, ch in enumerate(s):
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            instr = not instr
            continue
        if instr:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                objs.append(s[start:i + 1])
    return objs


def _salvage_steps(txt):
    """整体 json 解析失败时(常因视觉模型把长 JSON 截断)抢救:定位 "steps":[ 后,
    在数组区间里逐个配平扫出已写完的 step 对象。返回 [dict] 或 []。"""
    m = re.search(r'"steps"\s*:\s*\[', txt)
    arr = txt[m.end():] if m else txt
    out = []
    for o in _balanced_objects(arr):
        try:
            d = json.loads(o)
        except Exception:
            continue
        if isinstance(d, dict) and (d.get("body") or d.get("title")):
            out.append(d)
    return out


def _parse_steps(raw, id2label):
    """把模型输出解析成 steps[]:{tag,title,body,point_id,point_label}。
    多级兜底:整体 json → 贪婪 {…} → 截断抢救;全失败给友好提示(绝不把生 JSON 倒给用户)。"""
    txt = (raw or "").replace("```json", "").replace("```", "").strip()
    data = None
    try:
        data = json.loads(txt)
    except Exception:
        m = re.search(r"\{.*\}", txt, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                data = None
    raw_steps = None
    if isinstance(data, dict):
        raw_steps = data.get("steps")
    elif isinstance(data, list):
        raw_steps = data
    if not (isinstance(raw_steps, list) and raw_steps):
        raw_steps = _salvage_steps(txt)        # 截断/半截 JSON 抢救
    valid = list(id2label.keys())
    steps = []
    if isinstance(raw_steps, list):
        for i, s in enumerate(raw_steps):
            if not isinstance(s, dict):
                continue
            body = (s.get("body") or "").strip()
            if not body:
                continue
            pid = (s.get("point_id") or "").strip()
            if pid not in id2label:
                pid = valid[0] if valid else ""
            steps.append({"tag": (s.get("tag") or "思路").strip()[:8],
                          "title": (s.get("title") or f"第{i + 1}步").strip()[:40],
                          "body": body[:1200],
                          "point_id": pid,
                          "point_label": id2label.get(pid, "")})
    if not steps:
        pid = valid[0] if valid else ""
        # 抢救不出来:若残文是 JSON 残片就别倒给用户(那正是之前满屏生 JSON 的 bug),给友好提示;
        # 若是普通文字(模型没按 JSON 输出),就把它当一步正常显示。
        looks_json = txt.lstrip().startswith("{") or '"steps"' in txt or '"body"' in txt
        body = ("这道题的引导没生成好(可能太长被截断)。点下方「重新生成引导」再试一次。"
                if looks_json or not txt else txt[:1500])
        steps = [{"tag": "提示", "title": "引导生成异常",
                  "body": body, "point_id": pid, "point_label": id2label.get(pid, "")}]
    return steps


def check_guard(text):
    """轻量校验:命中疑似直接给答案的表述 → True(守护层疑似被突破)。"""
    if not text:
        return False
    return any(p.search(text) for p in _LEAK_PATTERNS)


# ---------- 业务主入口:分步引导 ----------
def generate_guidance(question_id, user_id=None, force=False):
    """检索方法论 → 注入守护层 + 题面图(读图)+ 答案锚点 → 生成【分步】引导(steps[])→ 校验。
    结果按题缓存:同一题秒出且每次一致;force=True 时重新生成并覆盖缓存。
    返回 {question_id, module, question_type, steps, retrieved_methods,
          guard_triggered, cached, stuck_presets}。题目不存在抛 ValueError(接入层映射 404)。"""
    q = _load_question(question_id)
    if not q:
        raise ValueError("题目不存在")
    presets = stuck_presets.presets_for(q["module"], q["question_type"])
    if not force:
        hit = _cache_get(q["id"])
        if hit:
            return {"question_id": str(q["id"]), "module": q["module"],
                    "question_type": q["question_type"], "steps": hit["steps"],
                    "retrieved_methods": hit["method_ids"],
                    "guard_triggered": hit["guard_triggered"], "cached": True,
                    "stuck_presets": presets}
    methods = _budget_methods(
        retrieve_methods(q["module"], q["question_type"], q["content"]))
    id2label = {m["id"]: m.get("label", "") for m in methods}
    system_text, blocks = assemble_steps(method_kb.guidance_system_prompt(), methods, q)
    try:
        raw = ai.guidance_complete(system_text, blocks, images=q.get("images"),
                                   scene="AI引导")
    except Exception:
        _logger.exception("guidance generate failed")
        raw = ""
    steps = _parse_steps(raw, id2label)
    joined = "\n\n".join(s["body"] for s in steps)
    triggered = check_guard(joined)
    method_ids = [m["id"] for m in methods]
    _cache_put(q["id"], steps, method_ids, triggered)
    _log(q["id"], user_id, joined, method_ids, triggered)
    return {"question_id": str(q["id"]), "module": q["module"],
            "question_type": q["question_type"], "steps": steps,
            "retrieved_methods": method_ids, "guard_triggered": triggered,
            "cached": False, "stuck_presets": presets}


# ---------- 重讲分支:针对具体卡点换说法讲透(用户点「还是没懂」/快捷卡点) ----------
_STUCK_DIRECTIVE = (
    "\n\n————\n学生卡在第 {step} 步,具体卡点是:\n「{stuck}」\n"
    "请【只针对这个卡点】换一种说法把它讲透:打比方、拆得更细、换个角度都行。\n"
    "硬性要求:禁止复述上一轮、禁止从头重走整个流程、只把这一个点说明白;"
    "仍然**不公布答案、不点破选项**;Markdown 分点,公式用 LaTeX。")


def explain_stuck(question_id, user_id, step_index, point_id, stuck_point):
    """换说法重讲:针对 stuck_point 把卡住的那个点讲透。返回 {body, guard_triggered, point_label}。"""
    q = _load_question(question_id)
    if not q:
        raise ValueError("题目不存在")
    methods = _budget_methods(
        retrieve_methods(q["module"], q["question_type"], q["content"]))
    # 优先锁定卡点对应的那条方法论;没有就用检索到的首条
    focus = next((m for m in methods if m["id"] == point_id), None) or \
        (methods[0] if methods else None)
    point_label = focus.get("label", "") if focus else ""
    profile = _load_profile(user_id)
    blocks = []
    if focus:
        blocks.append(_methods_block([focus]))
    if profile.strip():
        blocks.append("【学生能力画像(因材施教)】\n" + profile.strip())
    blocks.append(_question_block(q) +
                  _STUCK_DIRECTIVE.replace("{step}", str(int(step_index or 0) + 1))
                  .replace("{stuck}", (stuck_point or "没看懂这一步").strip()[:500]))
    try:
        body = ai.guidance_complete(method_kb.guidance_system_prompt(), blocks,
                                    images=q.get("images"), scene="AI引导重讲")[:2500]
    except Exception:
        _logger.exception("explain_stuck failed")
        body = ""
    triggered = check_guard(body)
    _log(q["id"], user_id, "[stuck] " + body, [point_id or ""], triggered)
    return {"body": body, "guard_triggered": triggered, "point_label": point_label}
