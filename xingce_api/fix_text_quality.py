# -*- coding: utf-8 -*-
"""一次性清洗已入库文本质量问题:
1) 题干答案括号跨行(「（」在行尾、「）」在下一行)→ 合回同一行;
2) 资料分析 material_text 开头混入的「题型说明 + 小节头 + 上一题选项」前缀 → 截掉,
   只保留真正的材料数据(从「…回答N～M题。」之后开始)。
不动答案/解析/向量(指纹取前80字、考点摘要均不受影响)。
用法: python fix_text_quality.py            # 试运行
      python fix_text_quality.py --apply    # 写库
"""
import sys
import re

from db import SessionLocal, Question, MaterialGroup

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_MAT_HEAD = re.compile(
    r'根据(?:以下|所给|下列|上述|下面)[^。\n]{0,20}'
    r'回答\s*\d+\s*[~～至\-－]\s*\d+\s*题[。\.]?')
_LEAD_OPT = re.compile(r'^(?:\s*[A-D][\.、．][^\n]*\n?)+')


def fix_paren(s):
    s = re.sub(r'（([^（）]*)）',
               lambda m: '（' + re.sub(r'\s*\n\s*', '', m.group(1)) + '）', s)
    s = re.sub(r'\(([^()]*)\)',
               lambda m: '(' + re.sub(r'\s*\n\s*', '', m.group(1)) + ')', s)
    s = re.sub(r'（(\s*)(?=[A-DＡ-Ｄ]\s*[.、．]|\s*$)', r'（）\1', s)
    return s


def fix_material(mt):
    if not mt:
        return mt
    m = _MAT_HEAD.search(mt)
    if m and m.start() < 400:        # 小节头/题型说明只会出现在材料最前面
        mt = mt[m.end():]
    mt = _LEAD_OPT.sub('', mt.lstrip())   # 兜底:清掉开头残留的上一题选项行
    return mt.strip()


def main():
    apply = "--apply" in sys.argv
    db = SessionLocal()
    n1 = n2 = 0
    for q in db.query(Question).filter(Question.status == 1).all():
        new = fix_paren(q.content or "")
        if new != (q.content or ""):
            n1 += 1
            if apply:
                q.content = new
    for g in db.query(MaterialGroup).all():
        new = fix_material(g.material_text or "")
        if new != (g.material_text or ""):
            n2 += 1
            if n2 <= 3:
                print(f"  mg{g.id} 材料新开头: {new[:70]}...")
            if apply:
                g.material_text = new
    if apply:
        db.commit()
    db.close()
    tag = "已写库" if apply else "试运行(加 --apply 写库)"
    print(f"\n括号跨行修复:{n1} 题  |  材料前缀清理:{n2} 组  [{tag}]")


if __name__ == "__main__":
    main()
