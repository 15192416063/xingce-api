# -*- coding: utf-8 -*-
"""AI 能力:DeepSeek 切题/分类/考点 + 本地 bge 向量。逻辑沿用已验证的 app主程序。"""
import re
import json
import functools
import config

# ---------- 懒加载,避免无 key 时启动就崩 ----------
@functools.lru_cache(maxsize=1)
def _llm():
    from langchain_openai import ChatOpenAI
    if not config.DEEPSEEK_API_KEY:
        raise RuntimeError("未配置 DEEPSEEK_API_KEY,无法切题/分类")
    return ChatOpenAI(model=config.LLM_MODEL, temperature=0,
                      api_key=config.DEEPSEEK_API_KEY,
                      base_url=config.DEEPSEEK_BASE_URL, timeout=60)


def embed(text: str):
    """调用 OpenAI 兼容的 embedding API(默认硅基流动 bge)。不再本地跑模型。"""
    import requests
    if not config.EMBED_KEY:
        raise RuntimeError("未配置 embedding API(请在 .env 设 XC_EMBED_KEY)")
    r = requests.post(
        config.EMBED_BASE_URL.rstrip("/") + "/embeddings",
        headers={"Authorization": "Bearer " + config.EMBED_KEY,
                 "Content-Type": "application/json"},
        json={"model": config.EMBED_MODEL, "input": (text or "")[:2000]},
        timeout=30)
    r.raise_for_status()
    return r.json()["data"][0]["embedding"]


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
    llm = _llm()
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
            all_qs.extend(_parse_json(llm.invoke(prompt).content))
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


# 细粒度题型参考(让 AI 分得更细,这是产品卖点)
FINE_TAXONOMY = """
言语理解 → 逻辑填空(实词辨析/成语辨析/关联词)、片段阅读(主旨概括/意图判断/细节判断/标题填入)、语句表达(语句排序/语句填空)
数量关系 → 数学运算(行程问题/工程问题/排列组合/概率/几何/经济利润/年龄/容斥/星期日期/最值)、数字推理
判断推理 → 图形推理(样式规律/位置规律/数量规律/属性规律/空间重构)、定义判断、类比推理、逻辑判断(翻译推理/真假推理/加强论证/削弱论证/前提假设/原因解释)
资料分析 → 增长率/增长量/比重/倍数/平均数/现期与基期/综合分析
常识判断 → 政治/法律/经济/历史人文/科技生活/地理国情
政治理论 → 时政/党的理论
"""


def classify(content: str) -> dict:
    prompt = f"""你是行测命题专家,精准分析下面这道题。严格输出JSON(不要解释):
{{"l1":"一级题型","l2":"二级题型","l3":"细分考点(尽量具体)","kp":"该题考查的具体知识点",
  "diff":1到3的难度,"summary":"题型-二级-细分考点-主题 的一句话精炼摘要(用于检索相似题)"}}
一级题型只能选:{L1}
二级与细分考点请参考下面的细粒度分类(l3要尽量落到最细一层):{FINE_TAXONOMY}
题目:{content}"""
    try:
        data = json.loads(_llm().invoke(prompt).content
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


def chat(message: str, history=None) -> str:
    """行测辅导对话:简洁、专业地答疑/讲解方法/分析考点。"""
    msgs = [{"role": "system",
             "content": "你是专业的行测辅导老师。回答简洁、实用,善于分析题目考点、"
                        "讲解解题方法与技巧。涉及具体题目时先点明题型与考点,再给思路。"
                        "不要长篇大论,控制在200字内。"}]
    for h in (history or [])[-6:]:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            msgs.append({"role": h["role"], "content": h["content"][:1000]})
    msgs.append({"role": "user", "content": message[:2000]})
    try:
        return _llm().invoke(msgs).content.strip()
    except Exception as e:
        return f"(AI 暂时不可用:{e})"
