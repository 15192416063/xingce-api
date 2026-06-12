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
                               "api_key": r.api_key, "model": r.model} for r in rows]
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


def _record(resp):
    try:
        u = getattr(resp, "usage_metadata", None) or {}
        stats.record_tokens(u.get("input_tokens", 0), u.get("output_tokens", 0))
    except Exception:
        pass
    return resp.content


def _invoke(messages):
    """统一 LLM 入口:按优先级逐渠道尝试 + token 记账。"""
    chans = _channels()
    if not chans:
        return _record(_llm().invoke(messages))   # 兼容:没配渠道走 .env
    last_err = None
    for ch in chans:
        try:
            return _record(_client(ch).invoke(messages))
        except Exception as e:
            last_err = e
            _mark_fail(ch["id"], e)
    raise RuntimeError(f"所有 AI 渠道均不可用,最后错误:{last_err}")


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
    vec = r.json()["data"][0]["embedding"]
    if len(_EMBED_CACHE) >= _EMBED_CACHE_MAX:
        _EMBED_CACHE.pop(next(iter(_EMBED_CACHE)))
    _EMBED_CACHE[key] = vec
    return vec


# ---------- 切题(分批+重叠,自适应排版) ----------
def _split_batches(text, batch_size=3000, overlap=200):
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
            all_qs.extend(_parse_json(_invoke(prompt)))
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
            for it in _parse_json(_invoke(prompt)):
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


def classify(content: str) -> dict:
    prompt = f"""分析这道行测题,严格输出JSON(不要解释):
{{"l1":"一级题型","l2":"二级题型","l3":"细分考点","kp":"具体知识点","diff":1到3,"summary":"题型-考点-主题一句话摘要(检索用)"}}
l1只能选:{L1}
l2/l3参考:{FINE_TAXONOMY}
题目:{content}"""
    try:
        data = json.loads(_invoke(prompt)
                          .replace("```json", "").replace("```", "").strip())
    except Exception:
        data = {}
    kp = data.get("kp") or data.get("l3", "")
    return {
        "l1": data.get("l1", ""), "l2": data.get("l2", ""),
        "l3": data.get("l3", ""), "kp": kp,
        "diff": int(data.get("diff", 2) or 2),
        "summary": data.get("summary", content[:40]),
    }


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


def chat(message: str, history=None) -> str:
    """行测辅导对话:简洁、专业地答疑/讲解方法/分析考点。"""
    msgs = [{"role": "system",
             "content": "你是专业的行测辅导老师。回答简洁、实用,善于分析题目考点、"
                        "讲解解题方法与技巧。涉及具体题目时先点明题型与考点,再给思路。"
                        "不要长篇大论,控制在200字内。"}]
    # 历史只带最近4条、每条截600字——再多对答疑帮助很小,token 白烧
    for h in (history or [])[-4:]:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            msgs.append({"role": h["role"], "content": h["content"][:600]})
    msgs.append({"role": "user", "content": message[:2000]})
    try:
        return _invoke(msgs).strip()
    except Exception as e:
        return f"(AI 暂时不可用:{e})"
