# -*- coding: utf-8 -*-
"""AI 能力:LLM 切题/分类/考点 + bge 向量。
LLM 走多渠道(ai_channel 表,管理面板可配),按优先级自动故障切换;
没配渠道时退回 .env 的 DEEPSEEK_API_KEY 单渠道,老部署零改动可用。"""
import re
import json
import time
import functools
import config
import stats


# ---------- 懒加载,避免无 key 时启动就崩 ----------
@functools.lru_cache(maxsize=1)
def _llm():
    from langchain_openai import ChatOpenAI
    if not config.DEEPSEEK_API_KEY:
        raise RuntimeError("未配置 AI 渠道(管理面板添加)或 DEEPSEEK_API_KEY")
    return ChatOpenAI(model=config.LLM_MODEL, temperature=0,
                      api_key=config.DEEPSEEK_API_KEY,
                      base_url=config.DEEPSEEK_BASE_URL, timeout=60)


# ---------- 多渠道:按优先级排队,坏了自动换下一个 ----------
_CH_CACHE = {"ts": 0.0, "items": None}   # 渠道列表缓存(60s 或管理员改动时刷新)
_CLIENTS = {}                            # (base_url, key, model) -> ChatOpenAI


def reload_channels():
    """管理面板增删改渠道后调用:下次请求重读数据库。"""
    _CH_CACHE["items"] = None
    _CLIENTS.clear()


def _channels():
    if _CH_CACHE["items"] is None or time.time() - _CH_CACHE["ts"] > 60:
        from db import SessionLocal, AiChannel
        db = SessionLocal()
        rows = (db.query(AiChannel)
                .filter(AiChannel.enabled == 1, AiChannel.api_key != "")
                .order_by(AiChannel.priority, AiChannel.id).all())
        _CH_CACHE["items"] = [{"id": r.id, "name": r.name, "base_url": r.base_url,
                               "api_key": r.api_key, "model": r.model,
                               "vision": r.supports_vision} for r in rows]
        _CH_CACHE["ts"] = time.time()
        db.close()
    return _CH_CACHE["items"]


def _client(ch):
    key = (ch["base_url"], ch["api_key"], ch["model"])
    if key not in _CLIENTS:
        from langchain_openai import ChatOpenAI
        _CLIENTS[key] = ChatOpenAI(model=ch["model"], temperature=0,
                                   api_key=ch["api_key"],
                                   base_url=ch["base_url"], timeout=60)
    return _CLIENTS[key]


def _mark_fail(ch_id, err):
    try:
        from db import SessionLocal, AiChannel
        db = SessionLocal()
        row = db.get(AiChannel, ch_id)
        if row:
            row.fail_count += 1
            row.last_error = str(err)[:255]
            db.commit()
        db.close()
    except Exception:
        pass


def _record(resp, scene="其他", channel=""):
    try:
        u = getattr(resp, "usage_metadata", None) or {}
        stats.record_tokens(u.get("input_tokens", 0), u.get("output_tokens", 0),
                            scene=scene, channel=channel)
    except Exception:
        pass
    return resp.content


def _invoke(messages, scene="其他"):
    """统一 LLM 入口:按优先级逐渠道尝试 + token 分账(用途+渠道)。"""
    chans = _channels()
    if not chans:
        return _record(_llm().invoke(messages), scene, "DeepSeek(.env)")  # .env 兜底
    last_err = None
    for ch in chans:
        try:
            return _record(_client(ch).invoke(messages), scene, ch["name"])
        except Exception as e:
            last_err = e
            _mark_fail(ch["id"], e)
    raise RuntimeError(f"所有 AI 渠道均不可用,最后错误:{last_err}")


def ocr_page(png_bytes: bytes) -> str:
    """视觉渠道识别一页扫描件 → Markdown 文本(按优先级故障切换)。"""
    import base64
    import requests
    chans = [c for c in _channels() if c.get("vision")]
    if not chans:
        raise RuntimeError("扫描版PDF(无文字层)。请在管理面板添加一个"
                           "「支持看图」的模型渠道(如 qwen-vl / glm-4v)后重传")
    b64 = base64.b64encode(png_bytes).decode()
    messages = [{"role": "user", "content": [
        {"type": "text",
         "text": "把这页行测试卷完整转写为 Markdown 文本:保留题号和选项标记"
                 "(A. B. C. D. 各占一行),忽略页眉页脚和水印。只输出转写内容,不要解释。"},
        {"type": "image_url",
         "image_url": {"url": "data:image/png;base64," + b64}}]}]
    last_err = None
    for ch in chans:
        try:
            r = requests.post(
                ch["base_url"].rstrip("/") + "/chat/completions",
                headers={"Authorization": "Bearer " + ch["api_key"]},
                json={"model": ch["model"], "messages": messages,
                      "temperature": 0, "max_tokens": 4000},
                timeout=180)
            r.raise_for_status()
            data = r.json()
            u = data.get("usage") or {}
            stats.record_tokens(u.get("prompt_tokens", 0),
                                u.get("completion_tokens", 0),
                                scene="扫描OCR", channel=ch["name"])
            return data["choices"][0]["message"]["content"] or ""
        except Exception as e:
            last_err = e
            _mark_fail(ch["id"], e)
    raise RuntimeError(f"视觉渠道全部不可用:{last_err}")


def test_channel(base_url, api_key, model):
    """管理面板「测试」按钮:发一条最小消息,返回(是否成功, 耗时ms/错误)。"""
    from langchain_openai import ChatOpenAI
    t0 = time.time()
    try:
        c = ChatOpenAI(model=model, temperature=0, api_key=api_key,
                       base_url=base_url, timeout=20, max_completion_tokens=8)
        c.invoke("回复OK")
        return True, f"{int((time.time() - t0) * 1000)}ms"
    except Exception as e:
        return False, str(e)[:200]


# ---------- embedding(带查询缓存:同样的话不重复花钱) ----------
_EMBED_CACHE = {}
_EMBED_CACHE_MAX = 500


def embed(text: str):
    """调用 OpenAI 兼容的 embedding API(默认硅基流动 bge)。不再本地跑模型。"""
    import requests
    key = (text or "")[:2000]
    if key in _EMBED_CACHE:
        return _EMBED_CACHE[key]
    if not config.EMBED_KEY:
        raise RuntimeError("未配置 embedding API(请在 .env 设 XC_EMBED_KEY)")
    r = requests.post(
        config.EMBED_BASE_URL.rstrip("/") + "/embeddings",
        headers={"Authorization": "Bearer " + config.EMBED_KEY,
                 "Content-Type": "application/json"},
        json={"model": config.EMBED_MODEL, "input": key},
        timeout=30)
    r.raise_for_status()
    data = r.json()
    vec = data["data"][0]["embedding"]
    try:
        u = data.get("usage") or {}
        stats.record_tokens(u.get("prompt_tokens", 0) or u.get("total_tokens", 0), 0,
                            scene="向量检索", channel="embedding:" + config.EMBED_MODEL)
    except Exception:
        pass
    if len(_EMBED_CACHE) >= _EMBED_CACHE_MAX:
        _EMBED_CACHE.pop(next(iter(_EMBED_CACHE)))
    _EMBED_CACHE[key] = vec
    return vec


# ---------- 切题(题号边界分批,不把题拦腰切断) ----------
_QNUM_RE = re.compile(r'(?m)^[\s#>*]{0,6}(\d{1,3})\s*[\.、．]')


def detect_qnums(text: str) -> dict:
    """全文里的题号锚点 {题号: 首次出现位置}(切题质量校验用)。"""
    out = {}
    for m in _QNUM_RE.finditer(text):
        out.setdefault(int(m.group(1)), m.start())
    return out


def _split_batches(text, batch_size=3000, overlap=200):
    """优先在题号边界下刀(整题进同一批);题号太少(排版怪)退回定长+重叠。"""
    anchors = [m.start() for m in _QNUM_RE.finditer(text)]
    if len(anchors) >= 5:
        out, bs = [], 0
        for pos in anchors:
            if pos - bs >= batch_size:
                out.append(text[bs:pos])
                bs = pos
        out.append(text[bs:])
        return [b for b in out if b.strip()]
    out, start = [], 0
    while start < len(text):
        out.append(text[start:start + batch_size])
        start += batch_size - overlap
    return out


def _parse_json(resp):
    resp = resp.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(resp)
    except json.JSONDecodeError:
        m = re.search(r'\[.*\]', resp, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return []
        return []


def split_questions(text, progress_cb=None):
    """返回 [{content, type, qnum}],跳过被截断的题。输入已是 Markdown(版面更干净)。"""
    all_qs, batches = [], _split_batches(text)
    for i, batch in enumerate(batches):
        prompt = f"""你是行测试卷切题专家。下面是行测试卷的 Markdown 片段(可能从中间截断)。
把其中**完整的**单道题提取出来。每道题输出三个字段:
- qnum: 题号(整数,找不到填0)
- content: 题目内容,必须**干净利落**:
  * 第一行是题干(去掉题号、页眉页脚、"第X页"、网址水印、Markdown标记如#*|)
  * 之后每个选项独占一行,统一写成 "A. 内容" 格式(B/C/D同)
  * 修复 PDF 换行导致的断句,删除多余空格;不加任何你自己的话
- type: 言语理解/常识判断/类比推理/逻辑判断/定义判断/数量关系/资料分析/图形推理/其他

规则:
1. 只提取完整的题(题干+选项齐全),开头/结尾被截断的忽略
2. 申论、材料写作、大段给定资料不提取
3. 只输出JSON数组,无任何解释:
[{{"qnum": 1, "content": "题干\\nA. …\\nB. …\\nC. …\\nD. …", "type": "题型"}}]
试卷片段:
{batch}
JSON:"""
        try:
            all_qs.extend(_parse_json(_invoke(prompt, scene="PDF切题")))
        except Exception:
            pass
        if progress_cb:
            progress_cb((i + 1) / len(batches))
    # 去重
    seen, res = set(), []
    for q in all_qs:
        key = re.sub(r'\s', '', q.get("content", "")[:30])
        if key and key not in seen:
            seen.add(key)
            res.append(q)
    return res


def parse_answer_key(text: str) -> dict:
    """从答案文本(粘贴或答案 PDF 提取)解析「题号→{answer, explanation}」映射。
    支持「1-5 ABCDA」「1.A 2.B」「1.【答案】A 解析:…」等常见排版。"""
    out = {}
    # 先本地解析最常见的紧凑排版(免 token):「1-5 ABCDA」与「1.A」
    for m in re.finditer(r'(\d{1,3})\s*[-—~至]\s*(\d{1,3})[::\s]+([A-D]{2,})', text):
        a, b, letters = int(m.group(1)), int(m.group(2)), m.group(3)
        if b - a + 1 == len(letters):
            for k, ch in enumerate(letters):
                out[a + k] = {"answer": ch, "explanation": ""}
    for m in re.finditer(r'(?<![-\d])(\d{1,3})\s*[\.、．::]\s*([A-D])(?![A-Z])', text):
        out.setdefault(int(m.group(1)), {"answer": m.group(2), "explanation": ""})
    if out:
        return out
    # 本地解析不出来(带解析的长文档)→ 分批让 LLM 提取
    for batch in _split_batches(text, batch_size=4000, overlap=100):
        prompt = f"""下面是行测答案/解析文档片段。提取每道题的题号、正确答案(A-D单字母)、
解析(没有就留空,有就保留原文、删页眉页脚)。只输出JSON数组,无解释:
[{{"qnum": 1, "answer": "A", "explanation": ""}}]
文档片段:
{batch}
JSON:"""
        try:
            for it in _parse_json(_invoke(prompt, scene="答案解析")):
                n = int(it.get("qnum", 0) or 0)
                a = (it.get("answer") or "").strip().upper()[:1]
                if n and a in "ABCD":
                    out.setdefault(n, {"answer": a,
                                       "explanation": (it.get("explanation") or "").strip()})
        except Exception:
            pass
    return out


# ---------- 分类+考点提炼(一次调用拿结构化结果) ----------
L1 = "/".join(config.CATEGORIES_L1)


# 细粒度题型参考(让 AI 分得更细,这是产品卖点;紧凑写法省 token——每道题都要发一遍)
FINE_TAXONOMY = """言语理解→逻辑填空(实词/成语/关联词)|片段阅读(主旨/意图/细节/标题)|语句表达(排序/填空)
数量关系→数学运算(行程/工程/排列组合/概率/几何/经济利润/年龄/容斥/日期/最值)|数字推理
判断推理→图形推理(样式/位置/数量/属性/空间重构)|定义判断|类比推理|逻辑判断(翻译/真假/加强/削弱/前提/解释)
资料分析→增长率|增长量|比重|倍数|平均数|基期现期|综合分析
常识判断→政治|法律|经济|历史人文|科技|地理
政治理论→时政|党的理论"""


def _norm_cls(data: dict, content: str) -> dict:
    kp = data.get("kp") or data.get("l3", "")
    try:
        diff = int(data.get("diff", 2) or 2)
    except (TypeError, ValueError):
        diff = 2
    return {
        "l1": data.get("l1", ""), "l2": data.get("l2", ""),
        "l3": data.get("l3", ""), "kp": kp,
        "diff": min(3, max(1, diff)),
        "summary": data.get("summary", content[:40]),
    }


def classify(content: str) -> dict:
    prompt = f"""分析这道行测题,严格输出JSON(不要解释):
{{"l1":"一级题型","l2":"二级题型","l3":"细分考点","kp":"具体知识点","diff":1到3,"summary":"题型-考点-主题一句话摘要(检索用)"}}
l1只能选:{L1}
l2/l3参考:{FINE_TAXONOMY}
题目:{content}"""
    try:
        data = json.loads(_invoke(prompt, scene="题目分类")
                          .replace("```json", "").replace("```", "").strip())
    except Exception:
        data = {}
    return _norm_cls(data, content)


def classify_batch(contents: list) -> list:
    """一次调用分类一批题(入库提速/省钱的关键)。返回与输入等长的列表;
    某题在返回里缺位时回退单题分类。"""
    if not contents:
        return []
    if len(contents) == 1:
        return [classify(contents[0])]
    items = "\n\n".join(f"【第{i + 1}题】\n{(c or '')[:1200]}"
                        for i, c in enumerate(contents))
    prompt = f"""逐题分析下面 {len(contents)} 道行测题。严格输出JSON数组,长度必须是{len(contents)},
i 为题序(1开始),顺序与输入一致,不要任何解释:
[{{"i":1,"l1":"一级题型","l2":"二级题型","l3":"细分考点","kp":"知识点","diff":1到3,"summary":"一句话摘要(检索用)"}}]
l1只能选:{L1}
l2/l3参考:{FINE_TAXONOMY}
{items}
JSON:"""
    by_i = {}
    try:
        for k, d in enumerate(_parse_json(_invoke(prompt, scene="批量入库分类"))):
            if isinstance(d, dict):
                try:
                    idx = int(d.get("i", k + 1) or (k + 1)) - 1
                except (TypeError, ValueError):
                    idx = k
                by_i.setdefault(idx, d)
    except Exception:
        pass
    out = []
    for i, c in enumerate(contents):
        if i in by_i:
            out.append(_norm_cls(by_i[i], c))
        else:
            try:
                out.append(classify(c))
            except Exception:
                out.append(_norm_cls({}, c))
    return out


# ---------- 零成本快速分类(词表命中就不调 LLM,省一次调用) ----------
# 「来几道图形推理」这类意图,本地词表足够准——LLM 只留给"贴了一道完整题"的场景
_L2_TO_L1 = {
    "图形推理": "判断推理", "定义判断": "判断推理", "类比推理": "判断推理",
    "逻辑判断": "判断推理", "翻译推理": "判断推理", "削弱": "判断推理", "加强": "判断推理",
    "逻辑填空": "言语理解", "片段阅读": "言语理解", "语句表达": "言语理解",
    "主旨": "言语理解", "成语辨析": "言语理解",
    "数学运算": "数量关系", "数字推理": "数量关系", "行程问题": "数量关系",
    "工程问题": "数量关系", "排列组合": "数量关系", "概率": "数量关系",
    "增长率": "资料分析", "增长量": "资料分析", "比重": "资料分析",
}
_L1_WORDS = {"言语理解": "言语理解", "言语": "言语理解",
             "数量关系": "数量关系", "数量": "数量关系",
             "判断推理": "判断推理", "资料分析": "资料分析", "资料": "资料分析",
             "常识判断": "常识判断", "常识": "常识判断",
             "政治理论": "政治理论", "时政": "政治理论"}


def quick_classify(message: str):
    """从消息里识别题型意图。命中返回分类 dict(不花钱),没命中返回 None。"""
    msg = message.strip()
    if len(msg) > 120:           # 长文本≈贴了道题,交给 LLM 精确分析
        return None
    for w, l1 in _L2_TO_L1.items():
        if w in msg:
            l2 = w if w in ("图形推理", "定义判断", "类比推理", "逻辑判断", "逻辑填空",
                            "片段阅读", "语句表达", "数学运算", "数字推理") else ""
            return {"l1": l1, "l2": l2, "l3": "", "kp": w, "diff": 2, "summary": w}
    for w, l1 in _L1_WORDS.items():
        if w in msg:
            return {"l1": l1, "l2": "", "l3": "", "kp": l1, "diff": 2, "summary": l1}
    return None


def solve(content: str, material: str = "") -> dict:
    """AI 解题:返回 {answer, explanation}。answer 不确定时为空串。
    生成的答案入库时标 origin=ai,前端明确提示「仅供参考」。"""
    mat = f"【材料】{material[:1500]}\n" if material else ""
    prompt = f"""你是行测名师。解答下面的题。严格输出JSON,不要任何其他内容:
{{"answer":"A","explanation":"解析:先点明考点,再给推理过程,150字内"}}
answer 只能是 A/B/C/D 单字母;若题目信息不足或你无法确定,answer 填空字符串。
{mat}【题目】{content[:2500]}
JSON:"""
    try:
        resp = _invoke(prompt, scene="AI解题").replace("```json", "").replace("```", "").strip()
        try:
            data = json.loads(resp)
        except json.JSONDecodeError:
            m = re.search(r'\{.*\}', resp, re.DOTALL)
            data = json.loads(m.group(0)) if m else {}
    except Exception:
        data = {}
    a = (data.get("answer") or "").strip().upper()[:1]
    return {"answer": a if a in "ABCD" else "",
            "explanation": (data.get("explanation") or "").strip()[:2000]}


def chat(message: str, history=None) -> str:
    """行测辅导对话:简洁、专业地答疑/讲解方法/分析考点。"""
    msgs = [{"role": "system",
             "content": "你是专业的行测辅导老师。回答简洁、实用,善于分析题目考点、"
                        "讲解解题方法与技巧。涉及具体题目时先点明题型与考点,再给思路。"
                        "不要长篇大论,控制在200字内。"
                        "重要:绝不要在回复里自己编写、列出或罗列完整的练习题或选项"
                        "(A/B/C/D 等)。当用户想做题时,系统会自动从真实题库调取带图真题"
                        "展示在你的回复下方;你只需用一两句话说明将为他推荐哪类题、"
                        "提示考点或方法即可,把题目本身交给系统展示。"}]
    # 历史只带最近4条、每条截600字——再多对答疑帮助很小,token 白烧
    for h in (history or [])[-4:]:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            msgs.append({"role": h["role"], "content": h["content"][:600]})
    msgs.append({"role": "user", "content": message[:2000]})
    try:
        return _invoke(msgs, scene="对话").strip()
    except Exception as e:
        return f"(AI 暂时不可用:{e})"


def _last_topic(history):
    """从对话历史里回溯最近一次提到的题型(连贯性兜底:'重新找'时沿用上一轮题型)。"""
    for h in reversed(history or []):
        if h.get("role") == "user":
            c = quick_classify((h.get("content") or "")[:120])
            if c:
                return c
    return None


def chat_reco(message: str, history=None) -> dict:
    """上下文感知:一次 LLM 调用读完整对话 → 出辅导回复 + 判断是否该给题、给哪种题型。
    返回 {reply, want, l1, l2, summary}。比"逐句关键词匹配"更连贯——
    "重新找/换个简单的/这个不对/来点别的"等都靠模型读上下文理解。
    双保险:若模型没按 JSON 输出,就用它那段话当回复 + 从上下文推断题型,绝不丢意图。"""
    # JSON 指令放最后(最强位置),不在后面再堆长文本,保证模型稳定吐 JSON
    system = (
        "你是专业的行测辅导老师,在和学生【连续多轮对话】。先读懂完整对话历史,"
        "再判断学生这句话在上下文里的真实意图:\n"
        "· '找错了/这个不对/重新找/换一道' = 换一道**同题型**的新题;\n"
        "· '换个简单的/太难了' = 同题型更简单;'来点资料分析' = 切换到资料分析;\n"
        "· 纯问方法/考点 = 只答疑、不给题(want=false)。\n"
        f"l1 只能从 [{L1}] 里选;l2 是细分题型(如 图形推理/逻辑判断/逻辑填空/增长率/比重 等)。\n"
        "reply 简洁口语、≤150字,绝不要自己编题或列 A/B/C/D 选项(题目由系统从真题库调取展示)。\n"
        "【只输出】下面这个 JSON,不要输出任何其它文字:\n"
        '{"reply":"给学生的话","want":true/false,"l1":"一级题型或空","l2":"二级题型或空","summary":"题型-考点检索摘要或空"}'
    )
    msgs = [{"role": "system", "content": system}]
    for h in (history or [])[-6:]:    # 带最近 6 轮上下文(连贯性的来源)
        if h.get("role") in ("user", "assistant") and h.get("content"):
            msgs.append({"role": h["role"], "content": h["content"][:600]})
    msgs.append({"role": "user", "content": message[:2000]})
    try:
        raw = _invoke(msgs, scene="对话").strip()
    except Exception as e:
        return {"reply": f"(AI 暂时不可用:{e})", "want": False, "l1": "", "l2": "", "summary": ""}
    resp = raw.replace("```json", "").replace("```", "").strip()
    data = {}
    try:
        data = json.loads(resp)
    except Exception:
        m = re.search(r'\{.*\}', resp, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                data = {}
    if not isinstance(data, dict) or "reply" not in data:
        # 模型只回了对话文本、没给 JSON → 用它当回复,意图靠上下文兜底推断
        c = quick_classify(message) or _last_topic(history)
        return {"reply": (raw[:600] or "好的。"), "want": bool(c),
                "l1": c["l1"] if c else "", "l2": (c.get("l2") if c else "") or "",
                "summary": (c.get("summary") if c else "") or message}
    l1 = (data.get("l1") or "").strip()
    if l1 not in config.CATEGORIES_L1:    # 题型越界 = 不推荐,避免乱调
        l1 = ""
    return {
        "reply": (data.get("reply") or "").strip() or "好的。",
        "want": bool(data.get("want")) and bool(l1),
        "l1": l1, "l2": (data.get("l2") or "").strip(),
        "summary": (data.get("summary") or "").strip(),
    }
