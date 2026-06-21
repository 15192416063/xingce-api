# -*- coding: utf-8 -*-
"""修复切题污染:个别"资料分析每组最后一题"的最后一个选项,被粘进了【下一段材料】的
小节头(如"四、根据以下资料,回答126～130题")及其材料文字。
做法:在题干里定位该小节头标记,从标记处截断(合法的题干+选项都在标记之前)。
不动答案/解析/向量(指纹取前80字、考点摘要均不受末尾截断影响)。

用法:
  python fix_split_contamination.py          # 试运行,只看会改哪些、改成什么
  python fix_split_contamination.py --apply   # 真正写库
"""
import sys
import re

from db import SessionLocal, Question

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 下一段材料的小节头:可选"四、"前缀 + "根据(以下/所给…)…回答 N～M 题"
MARK = re.compile(
    r'(?:[一二三四五六七八九十]\s*[、.]\s*)?'
    r'根据(?:以下|所给|下列|上述|下面|下图|下表)[^。\n]{0,20}'
    r'回答\s*\d+\s*[~～至\-－]\s*\d+\s*题')


def main():
    apply = "--apply" in sys.argv
    db = SessionLocal()
    qs = db.query(Question).filter(Question.status == 1).all()
    fixed = 0
    for q in qs:
        ct = q.content or ""
        m = MARK.search(ct)
        if not m:
            continue
        before = ct[:m.start()].rstrip(" \n、,，。/|")
        # 截断点太靠前(说明整段几乎都是下一材料,异常)→ 跳过保守不动
        if len(before) < 15:
            print(f"  [跳过] q{q.id} 截断点过早(len={len(before)}),需人工看")
            continue
        fixed += 1
        has_opts = len(re.findall(r'[A-D][\.、．]', before)) >= 2
        print(f"\nq{q.id} seq{q.seq_no} [{q.category_l1}/{q.category_l2}] "
              f"{len(ct)}→{len(before)}字 选项齐={has_opts}")
        print(f"   切掉:…{ct[m.start():m.start()+50].strip()}…")
        if apply:
            q.content = before
    if apply:
        db.commit()
        print(f"\n已修复并写库:{fixed} 题")
    else:
        print(f"\n[试运行] 将修复 {fixed} 题;确认无误后加 --apply 写库")
    db.close()


if __name__ == "__main__":
    main()
