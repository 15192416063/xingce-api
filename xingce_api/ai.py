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
import method_kb


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


def guidance_complete(system_text: str, user_blocks, scene: str = "AI引导") -> str:
    """引导式思维拆解的 LLM 出口:守护层走 system 角色,其余(方法论/画像/题目)
    按调用方给定顺序各自走 user 角色;复用多渠道故障切换 + token 分账。"""
    msgs = ([("system", system_text)] if system_text else [])
    msgs += [("human", b) for b in user_blocks if b]
    return _invoke(msgs, scene).strip()


def _has_vision() -> bool:
    """是否有可用视觉模型:管理面板配的"支持看图"渠道,或 .env 里的 VISION_KEY。"""
    try:
        if [c for c in _channels() if c.get("vision")]:
            return True
    except Exception:
        pass
    return bool(config.VISION_KEY)


def _vision_chat(system_text: str, user_text: str, image_paths=None,
                 scene: str = "资料分析读图", max_tokens: int = 1600,
                 image_urls=None) -> str:
    """带图的对话补全:把图片(磁盘路径 image_paths 或 data-URL image_urls)连同文字
    一起发给视觉模型(读图后再解题)。优先用管理面板的视觉渠道,否则回退 .env 硅基流动。"""
    import base64
    import requests
    chans = [c for c in _channels() if c.get("vision")]
    if not chans and config.VISION_KEY:
        chans = [{"id": 0, "name": "硅基流动VL(.env)",
                  "base_url": config.VISION_BASE_URL, "api_key": config.VISION_KEY,
                  "model": config.VISION_MODEL, "vision": 1}]
    if not chans:
        raise RuntimeError("未配置视觉渠道")
    content = [{"type": "text", "text": user_text}]
    for p in (image_paths or [])[:4]:        # 最多带 4 张图,防超长
        try:
            with open(p, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            content.append({"type": "image_url",
                            "image_url": {"url": "data:image/png;base64," + b64}})
        except Exception:
            continue
    for u in (image_urls or [])[:4]:         # 浏览器上传的 data-URL,直接透传
        if u:
            content.append({"type": "image_url", "image_url": {"url": u}})
    if len(content) == 1:                    # 一张图都没读到 → 不值得走视觉
        raise RuntimeError("无可用图片")
    messages = ([{"role": "system", "content": system_text}] if system_text else [])
    messages.append({"role": "user", "content": content})
    last_err = None
    for ch in chans:
        try:
            r = requests.post(
                ch["base_url"].rstrip("/") + "/chat/completions",
                headers={"Authorization": "Bearer " + ch["api_key"]},
                json={"model": ch["model"], "messages": messages,
                      "temperature": 0.2, "max_tokens": max_tokens,
                      "frequency_penalty": 0.6, "presence_penalty": 0.3},
                timeout=180)
            r.raise_for_status()
            data = r.json()
            u = data.get("usage") or {}
            stats.record_tokens(u.get("prompt_tokens", 0),
                                u.get("completion_tokens", 0),
                                scene=scene, channel="vision:" + ch["model"])
            return data["choices"][0]["message"]["content"] or ""
        except Exception as e:
            last_err = e
            if ch.get("id"):
                _mark_fail(ch["id"], e)
    raise RuntimeError(f"视觉渠道不可用:{last_err}")


def _complete(sys_text: str, user_text: str, images, scene: str) -> str:
    """统一出口:本题带图且有视觉模型 → 读图解析;否则/失败 → 纯文字解析。"""
    if images and _has_vision():
        try:
            return _vision_chat(sys_text, user_text, images, scene)
        except Exception:
            pass   # 视觉失败绝不能让解析整体挂掉 → 回退纯文字
    msgs = ([("system", sys_text)] if sys_text else []) + [("human", user_text)]
    return _invoke(msgs, scene)


def chat_vision(message: str, image_data_url: str) -> str:
    """对话框传图:视觉模型读图(通常是一道行测题)并按方法论作答。
    image_data_url = 浏览器传来的 data:image/...;base64,...。无视觉渠道时抛错由上层兜底。"""
    sys_text = (method_kb.system_prompt() +
                "\n\n学生发来一张图片,通常是一道行测题(可能含题干、选项、图形或图表)。"
                "请先看懂图中内容,再按方法论解答:点明题型与考点 → 推理/计算过程 → 给出答案。"
                "Markdown 分点、公式用 LaTeX、直接简洁、不试错不重复;图形推理只讲规律、不臆测细节。\n"
                "⚠️诚实第一:你是【直接读图】作答,没有标准答案兜底,可能看错。"
                "**图形推理(尤其立体拼合、空间折叠、截面)是你最容易出错的题型**——"
                "这类题如果没有十足把握,要**明确说出'我不太确定,这类立体/图形题读图容易出错'**,"
                "给出倾向性答案即可,**不要把没把握的答案说得斩钉截铁**;宁可坦白不确定,也不要误导。")
    reply = _vision_chat(
        sys_text,
        message or "请解答图中的行测题;若图中没有完整题目,就描述图片内容并讲相关考点。",
        image_paths=None, scene="对话读图", max_tokens=1800,
        image_urls=[image_data_url]).strip()[:2400]
    # 自解无官方答案兜底,统一加一句诚实提示:引导用户回题库看锚定标准答案的解析
    return reply + ("\n\n> 📌 以上是我**直接读图**的作答,可能出错。若这是真题,"
                    "建议在题库搜到原题、点「AI 解答」查看**锚定官方答案**的解析(那个一定对)。")


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


def solve(content: str, material: str = "", l1: str = "", l2: str = "",
          images=None) -> dict:
    """AI 解题:返回 {answer, explanation}。answer 不确定时为空串。
    注入该题型方法论辅助推理;本题带图且有视觉模型时读图再解;
    生成的答案入库时标 origin=ai,前端明确提示「仅供参考」。"""
    mat = f"【材料】{material[:1500]}\n" if material else ""
    method = method_kb.method_context(l1, l2, content)
    mblock = f"【本题方法论(据此推理)】\n{method}\n" if method else ""
    prompt = f"""你是行测名师。解答下面的题(若附有图表/图形图片,先准确读图再解)。
严格输出JSON,不要任何其他内容:
{{"answer":"A","explanation":"解析:依方法论先点考点,再给推理过程,150字内"}}
answer 只能是 A/B/C/D 单字母;若题目信息不足或你无法确定,answer 填空字符串。
注意:这是 AI 自行解答、没有标准答案兜底。**图形推理(尤其立体拼合、空间折叠、截面)读图极易出错**——
这类题没有十足把握时,**宁可把 answer 填空字符串(交给人工/官方答案),也不要硬猜一个可能错的字母**。
{mblock}{mat}【题目】{content[:2500]}
JSON:"""
    try:
        resp = _complete("", prompt, images, "AI解题").replace("```json", "").replace("```", "").strip()
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


def explain(content: str, answer: str, material: str = "",
            l1: str = "", l2: str = "", images=None) -> str:
    """已知正确答案,按【解析方法论框架】生成解析(不让 AI 猜答案 → 解析对着正确答案讲)。
    注入守护层 system prompt + 该题型方法论,保证有理有据、思路一致;
    用自己的话讲、不复制教辅原文(避免版权);入库后全员共享、不重复花 token。"""
    mat = (f"【材料(解题依据,务必引用其中的具体数据)】\n{material[:1800]}\n"
           if material else "")
    sys = method_kb.system_prompt()
    method = method_kb.method_context(l1, l2, content)
    if method:
        sys += "\n\n【本题方法论(严格据此组织解析,不要自创解法)】\n" + method
    user = (f"请按方法论给出这道题的解析,像老师板书一样把过程做出来。\n"
            f"【标准答案(题库官方答案,铁定正确)】{answer}\n{mat}【题目】{content[:2500]}\n"
            f"⚠️硬性要求:本题答案**就是 {answer}**,你的唯一任务是讲清"
            f"**为什么是 {answer}**;**严禁给出、改成或暗示其他选项**;"
            f"若你读图/推理的结论与 {answer} 冲突,那是你想错了(图形/立体类尤其容易看错),"
            f"请以 {answer} 为准重新组织讲解,绝不要在解析里写'答案应为X''我认为是X'之类与 {answer} 不一致的话。\n"
            f"按系统设定的格式输出;讲清为什么是 {answer}、关键干扰项为何不对、点出考点。\n"
            "资料分析:必须先在材料**或所附图表图片**中定位并写出具体数据,再列出公式与算式"
            "一步步算到最终结果,不要只说思路不算数;只有数据确实无从获取时才讲思路、**绝不编造数字**。\n"
            "排版:Markdown 分点呈现,不要写成一大段;所有公式、算式一律用 LaTeX"
            "(行内 $...$、独立 $$...$$),不要把公式写成纯文字。\n"
            "务必**直接、简洁**:判断出规律/思路后直接讲清并给出答案,"
            "**绝不要罗列多种猜测、绝不要逐步试错、绝不要重复同样的话、绝不要写"
            "'重新审视/重新分析/以上不成立/换角度'**。\n"
            "图形推理特别注意:**只用三五句话**说清考查哪一类规律(对称/数量/位置/样式/空间重构)、"
            "以及答案为什么对,**不要分很多步、不要逐幅图枚举坐标或反复试错**;"
            "看不准就只给规律方向和答案,绝不硬编每幅图的细节。\n"
            "用你自己的话讲,不要复制任何教辅/书本原文。")
    try:
        return _complete(sys, user, images, "AI解析").strip()[:2500]
    except Exception:
        return ""


ERROR_TAGS = ("概念不清", "审题失误", "计算错误", "时间不够", "蒙错")


def explain_wrong(content: str, correct: str, user_answer: str, topic: str,
                  l1: str = "", l2: str = "", material: str = "", images=None) -> dict:
    """讲错题:先按方法论给出【完整、有理有据的解析】(资料分析要定位材料数据+列式计算,
    对标官方解析质量),再针对用户的错误作答讲【为什么错】,并顺手判一个错因标签。
    注入守护层 system prompt + 题型方法论 + 材料数据。
    一次调用同时拿到讲解 + error_tag(不拆多次请求,省 token)。
    返回 {explanation, error_tag}。同类提醒(历史错误次数)由调用方本地拼。"""
    sys = method_kb.system_prompt()
    method = method_kb.method_context(l1, l2, content)
    if method:
        sys += "\n\n【本题方法论(严格据此分步解题)】\n" + method
    mat = (f"【材料(解题依据,务必引用其中的具体数据)】\n{material[:1800]}\n"
           if material else "")
    user = f"""这是一道用户【做错】的题。请输出两部分,中间空一行:

一、【完整解析】严格按方法论把题做出来,像老师板书一样有理有据:
 - 资料分析:第一步先在材料或所附图表图片中定位相关数据并写出具体数字;第二步写出公式与算式;第三步一步步算出结果;
 - 言语/判断/数量等:按方法论步骤推理,每个关键判断给出依据;
 - 结尾明确写"故正确答案为 {correct}"。
二、【你为什么错】针对"他选了 {user_answer}"具体分析,指出他错在哪一步、踩了什么坑(不要泛泛复述答案)。

{mat}【题目】{content[:2500]}
【正确答案(题库官方答案,铁定正确)】{correct}
【他的作答】{user_answer}
【考点】{topic}

⚠️硬性要求:本题正确答案**就是 {correct}**,严禁在解析里给出、改成或暗示其他选项;
若你读图/推理的结论与 {correct} 冲突(图形/立体类尤其容易看错),那是你想错了,请以 {correct} 为准重讲。

要求:解析详实、条理清楚但**简洁不啰嗦**,**Markdown 分点**呈现;所有公式与算式一律用 LaTeX
(行内 $...$、独立 $$...$$),不要把公式写成纯文字;
**绝不要罗列多种猜测、绝不要逐步试错、绝不要重复同样的话、绝不要写'重新审视/重新分析/以上不成立/换角度'**;
图形推理:**只用三五句话**说清考查哪类规律(对称/数量/位置/样式/空间重构)和答案为什么对,
**不要分很多步、不要逐幅图枚举坐标或反复试错**,看不准就只给规律方向和答案、不硬编细节;
只有当数据确实无从获取时,才依据标准答案讲清思路与公式,**绝不编造数字**。
最后**另起一行只输出**这个 JSON(打错因标签,前面不要带它):
{{"error_tag":"概念不清|审题失误|计算错误|时间不够|蒙错"}}"""
    try:
        raw = _complete(sys, user, images, "AI讲题").strip()
    except Exception:
        return {"explanation": "", "error_tag": ""}
    tag, text = "", raw
    m = re.search(r'\{[^{}]*error_tag[^{}]*\}', raw)
    if m:
        try:
            tag = (json.loads(m.group(0)).get("error_tag") or "").strip()
        except Exception:
            tag = ""
        text = raw[:m.start()].strip()
    text = text.replace("```json", "").replace("```", "").strip()
    return {"explanation": text[:2500], "error_tag": tag if tag in ERROR_TAGS else ""}


def update_memory(profile: str, recent_text: str) -> str:
    """把最近对话里关于学生的【长期有用】信息合并进学习档案(Markdown)。
    只记稳定事实(考试经历/薄弱擅长/目标习惯/偏好),删过时的,不记一次性闲聊。"""
    prompt = f"""你在维护一名学生的"学习档案"(Markdown,用于个性化辅导)。
下面是【现有档案】和【最近对话】。把对话里关于学生的**长期有用**信息合并进档案:
是否考过公务员/事业编及成绩、薄弱/擅长题型、学习目标与习惯、偏好等。
规则:只保留稳定事实;删除过时信息;不要记一次性闲聊;档案精简(≤400字)、分点。
只输出更新后的 Markdown 档案本身,不要任何解释。
【现有档案】
{profile or '(空)'}
【最近对话】
{recent_text[:3000]}
更新后的档案:"""
    try:
        out = _invoke(prompt, scene="用户档案").strip()
        return out[:1500] if out else profile
    except Exception:
        return profile


def ask_about_question(content, answer, explanation="", material="", images=None,
                       history=None, message="", l1="", l2=""):
    """针对【某一道具体题目】的多轮追问答疑:始终带着题干/标准答案/解析/方法论作上下文,
    AI 不跑题到别的题。带图的题(图形/资料分析)走视觉模型读图。返回回复文本。"""
    sysp = method_kb.system_prompt()
    method = method_kb.method_context(l1, l2, content)
    ctx = ["你正在就【下面这道具体的题】为学生答疑,所有回答都要紧扣这道题、不要跑题到别的题。",
           f"【题目】\n{content[:2200]}",
           f"【标准答案(题库官方答案,铁定正确)】{answer or '(暂无)'}"]
    if answer:
        ctx.append(f"⚠️本题答案就是 {answer},严禁给出、改成或暗示其他选项;"
                   f"若你读图/推理与 {answer} 冲突(图形/立体类尤其容易看错),以 {answer} 为准,是你想错了。")
    if material:
        ctx.append(f"【材料】{material[:1500]}")
    if explanation:
        ctx.append(f"【本题已有解析】{explanation[:1500]}")
    if method:
        ctx.append(f"【本题方法论】{method}")
    sys_text = (sysp + "\n\n" + "\n".join(ctx) +
                "\n\n回答要求:Markdown 分点、公式用 LaTeX(行内 $...$、独立 $$...$$);"
                "直接简洁、不试错不重复;图形推理只讲规律、不臆测看不到的细节;不编造数字。")
    hist = ""
    for h in (history or [])[-6:]:
        who = "学生" if h.get("role") == "user" else "老师"
        c = (h.get("content") or "")[:600]
        if c:
            hist += f"{who}:{c}\n"
    user_text = ((f"【之前的追问记录】\n{hist}\n" if hist else "")
                 + f"【学生现在问】{message[:1500]}")
    try:
        return _complete(sys_text, user_text, images, "题目追问").strip()[:2500]
    except Exception as e:
        return f"(AI 暂时不可用:{e})"


def chat(message: str, history=None) -> str:
    """行测辅导对话:简洁、专业地答疑/讲解方法/分析考点。"""
    msgs = [{"role": "system",
             "content": "你是专业的行测辅导老师。回答简洁、实用,善于分析题目考点、"
                        "讲解解题方法与技巧。涉及具体题目时先点明题型与考点,再给思路。\n"
                        "格式要求:用 Markdown **分点**作答,不要写成一大段;公式用 LaTeX"
                        "(行内 $...$、独立 $$...$$);直接给最终结论,**不要展示试错/自我纠正过程**;"
                        "图形推理只讲方法规律、不臆测看不到的具体图形。\n"
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


def chat_reco(message: str, history=None, profile: str = "", learning: str = "") -> dict:
    """上下文感知:一次 LLM 调用读完整对话 → 出辅导回复 + 判断是否该给题、给哪种题型。
    profile=该用户"学习档案"(类 Claude memory,定性、慢更新);
    learning=该用户"实时学情"(正确率/各模块进度/错题分布,做题数据、每次现算)。
    返回 {reply, want, l1, l2, summary}。"""
    persona = (
        f"【你已了解的学生情况】\n{profile.strip()}\n请结合这些因材施教(不必每次提起)。\n"
        if profile.strip() else
        "【这是新学生,你还不了解TA】可在回复里自然、友好地问一两句以便因材施教"
        "(如:之前考过公务员/事业编吗?成绩如何?觉得哪部分最没把握?),"
        "学生不愿答就别追问、正常帮TA答疑/练题。\n")
    if learning.strip():
        persona += (f"【该学生实时学情(做题数据)】\n{learning.strip()}\n"
                    "回应时可据此给针对性建议:正确率低/错题多的模块多提醒、多推该类题。\n")
    # JSON 指令放最后(最强位置),保证模型稳定吐 JSON
    system = (
        "你是专业的行测辅导老师,在和学生【连续多轮对话】。先读懂完整对话历史与意图。\n"
        + persona +
        "意图判断:\n"
        "· '找错了/这个不对/重新找/换一道' = 换一道**同题型**的新题;\n"
        "· '换个简单的/太难了' = 同题型更简单;'来点资料分析' = 切换题型;\n"
        "· 问方法/考点/概念 = 只答疑不给题(want=false);要题/练习 = want=true。\n"
        f"l1 只能从 [{L1}] 里选;l2 是细分题型(如 图形推理/逻辑判断/逻辑填空/增长率/比重 等)。\n"
        "reply 写法(关键,务必遵守):\n"
        "· 用 Markdown 组织:加粗小标题 + 有序/无序列表**分点**,每点一行,"
        "**绝不要写成一大段**密密麻麻的文字;\n"
        "· 公式一律用 LaTeX:行内用 $...$,独立成行用 $$...$$,不要把公式当普通文字写。"
        "例:$比重=\\dfrac{部分量}{整体量}\\times100\\%$;\n"
        "· 直接给出**最终、正确、条理清晰**的讲解;**绝不展示试错或自我纠正过程**"
        "(不准出现'分析有误''重新分析''我再想想''等一下'之类的字样);\n"
        "· 图形推理:你看不到具体图形,因此只讲**判型方法、常见规律与口诀、从哪下手排查**,"
        "**不要臆测或编造**对某张具体图的逐一分析;\n"
        "· 学生在**问方法/考点/概念**时,把'为什么'讲透,分点、可到 500 字;\n"
        "· 学生**要题/练习**时,只简短一两句说明给TA推荐哪类题(题目由系统从真题库调取),"
        "**绝不要自己编题或列 A/B/C/D 选项**。\n"
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
        return {"reply": (raw[:3000] or "好的。"), "want": bool(c),
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
