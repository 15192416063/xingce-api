# -*- coding: utf-8 -*-
"""行测解析方法论框架的加载与路由。
- 读取 行测解析框架/ 下的 system_prompt.md + 三个知识库 JSON;
- 按题目的 (模块 l1, 题型 l2, 题干) 路由出"本题方法论"注入文本:
    · 图形推理 → 判型路由 router + 命中的维度方法
    · 资料分析 → 命中的公式族 + 关联速算 + 黄金法则/通用陷阱
    · 其余题型 → 命中的题型 entry(核心思路/步骤/技巧/陷阱)
- 文件按 mtime 自动热重载(你随时删改框架,无需重启即可生效)。
"""
import os
import re
import json

import config

_CACHE = {"mtime": -1, "sys": "", "main": [], "tx": {}, "zl": {}}
_MAX = 2200   # 注入文本上限,避免撑爆 prompt


def _paths():
    d = config.KB_DIR
    return {"sys": os.path.join(d, "system_prompt.md"),
            "main": os.path.join(d, "xingce_method_kb.json"),
            "tx": os.path.join(d, "tuxing_tuili_kb.json"),
            "zl": os.path.join(d, "ziliao_fenxi_kb.json")}


def _latest_mtime(paths):
    t = -1
    for p in paths.values():
        try:
            t = max(t, os.path.getmtime(p))
        except OSError:
            pass
    return t


def _load():
    paths = _paths()
    mt = _latest_mtime(paths)
    if _CACHE["mtime"] == mt:          # 文件没动,用缓存
        return
    def _json(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    try:
        with open(paths["sys"], encoding="utf-8") as f:
            sysp = f.read().strip()
    except Exception:
        sysp = ""
    _CACHE.update({"mtime": mt, "sys": sysp,
                   "main": _json(paths["main"]).get("entries", []),
                   "tx": _json(paths["tx"]), "zl": _json(paths["zl"])})


def loaded_ok():
    _load()
    return bool(_CACHE["sys"] or _CACHE["main"] or _CACHE["tx"] or _CACHE["zl"])


def system_prompt():
    _load()
    return _CACHE["sys"] or (
        "你是行测解析助手。系统会给出该题的标准答案,你的职责是解释这个答案"
        "为何正确、其他选项为何错,讲清解题思路。答案以题库为准,严禁虚构知识点。")


# ---------- 把 entry 字段拼成可注入文本 ----------
def _fmt(label, val):
    if not val:
        return ""
    if isinstance(val, list):
        lines = []
        for it in val:
            if isinstance(it, dict):
                lines.append("- " + "；".join(f"{k}:{v}" for k, v in it.items()))
            else:
                lines.append("- " + str(it))
        body = "\n".join(lines)
    elif isinstance(val, dict):
        body = "\n".join(f"- {k}:{v}" for k, v in val.items())
    else:
        body = str(val)
    return f"{label}:\n{body}"


def _overlap(text, keywords):
    return sum(1 for k in (keywords or []) if k and k in text)


def _join(parts):
    return "\n".join(p for p in parts if p).strip()[:_MAX]


# ---------- 各模块路由 ----------
def _main_entry(l1, l2, content):
    best, score = None, 0
    for e in _CACHE["main"]:
        s = 0
        if e.get("module") and e["module"] == l1:
            s += 2
        short = re.split(r'[（(/]', e.get("type", ""))[0]
        if l2 and short and (short in l2 or l2 in short):
            s += 6
        s += _overlap(content, e.get("keywords"))
        if s > score:
            best, score = e, s
    return best


def _tx_context(content):
    tx = _CACHE["tx"] or {}
    router = tx.get("router", {})
    parts = ["【图形推理 · 判型路由(始终遵循)】",
             router.get("core_idea", ""),
             _fmt("判型口诀", router.get("judge_mnemonic")),
             _fmt("排查优先级", router.get("priority_order")),
             _fmt("通用陷阱", router.get("general_traps"))]
    ents = tx.get("entries", [])
    hits = sorted(ents, key=lambda e: _overlap(content, e.get("keywords")),
                  reverse=True)
    hits = [e for e in hits if _overlap(content, e.get("keywords")) > 0][:2]
    if hits:
        parts.append("\n【候选维度方法】")
        for e in hits:
            parts.append(f"◆ {e.get('dimension')} · {e.get('subtype')}")
            parts.append(_fmt("步骤", e.get("method_steps")))
            parts.append(_fmt("技巧", e.get("techniques")))
            parts.append(_fmt("陷阱", e.get("traps")))
    else:
        parts.append("(图形特征文字不足时,按口诀依次排查:对称→数量→位置→样式→空间重构)")
    return _join(parts)


def _zl_context(content):
    zl = _CACHE["zl"] or {}
    router = zl.get("router", {})
    parts = ["【资料分析 · 公式路由】", _fmt("黄金法则", router.get("golden_rules"))]
    fents = zl.get("formula_entries", [])
    speeds = {s.get("id"): s for s in zl.get("speedup_entries", [])}
    hits = sorted(fents, key=lambda e: _overlap(content, e.get("keywords")),
                  reverse=True)
    hits = [e for e in hits if _overlap(content, e.get("keywords")) > 0][:2]
    used = []
    for e in hits:
        parts.append(f"◆ 公式族:{e.get('family')}")
        parts.append(_fmt("公式", e.get("formulas")))
        parts.append(_fmt("步骤", e.get("method_steps")))
        for sid in e.get("speedups", []):
            if sid in speeds and sid not in used:
                used.append(sid)
                parts.append(_fmt(f"速算 · {speeds[sid].get('name')}",
                                  speeds[sid].get("how")))
    if not hits:
        parts.append("(先按问法定位公式族:增长率/增长量/基期现期/比重/倍数/平均数/拉动贡献)")
    parts.append(_fmt("通用陷阱", zl.get("global_traps")))
    return _join(parts)


def method_context(l1="", l2="", content=""):
    """返回该题应注入的'本题方法论'文本(命不中返回空串)。"""
    _load()
    l1, l2, content = l1 or "", l2 or "", content or ""
    if "图形" in l2 or "图形" in l1:
        return _tx_context(content)
    if "资料分析" in l2 or "资料分析" in l1:
        return _zl_context(content)
    e = _main_entry(l1, l2, content)
    if not e:
        return ""
    return _join([f"【本题题型】{e.get('module')} · {e.get('type')}",
                  _fmt("核心思路", e.get("core_idea")),
                  _fmt("解题步骤", e.get("method_steps")),
                  _fmt("技巧", e.get("techniques")),
                  _fmt("常见陷阱", e.get("traps"))])
