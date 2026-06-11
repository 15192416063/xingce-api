# -*- coding: utf-8 -*-
"""AI 能力:DeepSeek 切题/分类/考点 + 本地 bge 向量。逻辑沿用已验证的 app主程序。"""
import re
import json
import functools
import config
import stats


# ---------- 懒加载,避免无 key 时启动就崩 ----------
@functools.lru_cache(maxsize=1)
def _llm():
    from langchain_openai import ChatOpenAI
    if not config.DEEPSEEK_API_KEY:
        raise RuntimeError("未配置 DEEPSEEK_API_KEY,无法切题/分类")
    return ChatOpenAI(model=config.LLM_MODEL, temperature=0,
                      api_key=config.DEEPSEEK_API_KEY,
                      base_url=config.DEEPSEEK_BASE_URL, timeout=60)


def _invoke(messages):
    """统一 LLM 入口:调用 + token 记账(成本全部进统计面板)。"""
    resp = _llm().invoke(messages)
    try:
        u = getattr(resp, "usage_metadata", None) or {}
        stats.record_tokens(u.get("input_tokens", 0), u.get("output_tokens", 0))
    except Exception:
        pass
    return resp.content


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
    """返回 [{content, type}],跳过被截断的题。"""
    all_qs, batches = [], _split_batches(text)
    for i, batch in enumerate(batches):
        prompt = f"""你是行测试卷解析专家。下面是一份行测试卷的一段文字(可能从中间截断)。
请把其中**完整的**单道题目提取出来,每道题包含题干和它的所有选项。
注意:题号可能在行首(如"1 "、"1."、"1、"),也可能漂在题干中;选项标记可能是"A."或"A、"。
要求:
1. 只提取完整的题(题干+选项齐全),被截断的忽略。
2. 判断题型,type 只能填:言语理解/常识判断/类比推理/逻辑判断/数量关系/资料分析/图形推理/其他
3. 图形推理题也标出来,type填"图形推理"
4. 申论、材料写作不要提取
5. 只输出JSON数组,不要任何解释:
[{{"content": "完整题目原文", "type": "题型"}}]
试卷文字:
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
