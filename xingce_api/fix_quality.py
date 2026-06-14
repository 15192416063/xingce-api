# -*- coding: utf-8 -*-
r"""一次性数据质检修复(对公共题):
1) 切坏的内容(开头粘了上一题选项)→ 规则清洗,只留真题干+本题选项
2) "选图/坐标图"残缺题(问'哪个图能反映…'但没抠到图选项)→ 标 status=0 待确认,撤出题池
3) "选图"但有图选项的 → 归到数量关系(LLM 易把它误判图形推理,故强制)
4) 其余被清洗过的普通题 → 用清洗后的干净内容重新分类(批/单题)
用法: python fix_quality.py
"""
import sys
import re
import sqlite3

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import config
import ai
import textutil

_OPT_START = re.compile(r'^\s*[A-DＡ-Ｄ][、.．]')
_QNUM = re.compile(r'(?m)^\s*\d{1,3}\s*[、.．]\s*')
_SELIMG = re.compile(r'哪[个种幅一].{0,6}(图形|坐标图|图象|曲线图|示意图|图)')


def main():
    db = sqlite3.connect(config.DB_URL[len("sqlite:///"):])
    c = db.cursor()
    rows = c.execute("SELECT id,has_image,content FROM question "
                     "WHERE scope='public' AND status!=2").fetchall()
    # 计算每题清洗后的内容(仅对"以选项开头且后面有题号"的)
    cleaned = {}
    for qid, himg, body in rows:
        b = body or ""
        if _OPT_START.match(b):
            m = _QNUM.search(b)
            if m and m.start() > 0:
                cleaned[qid] = textutil.clean_text(b[m.end():].strip())

    waitconfirm, forcemath, reclassify = [], [], []
    for qid, himg, body in rows:
        eff = cleaned.get(qid, body or "")
        if _SELIMG.search(eff):
            (waitconfirm if not himg else forcemath).append(qid)
        elif qid in cleaned:
            reclassify.append(qid)

    # 1) 应用内容清洗
    for qid, cc in cleaned.items():
        c.execute("UPDATE question SET content=? WHERE id=?", (cc, qid))
    # 2) 选图残缺 → 待确认(撤出题池)
    if waitconfirm:
        c.execute("UPDATE question SET status=0 WHERE id IN (%s)"
                  % ",".join(map(str, waitconfirm)))
    # 3) 选图但有图 → 强制归数量关系
    if forcemath:
        c.execute("UPDATE question SET category_l1='数量关系',category_l2='数学运算',"
                  "knowledge_point='' WHERE id IN (%s)" % ",".join(map(str, forcemath)))
    db.commit()

    # 4) 其余清洗过的普通题:用干净内容重新分类
    done = 0
    for qid in reclassify:
        content = c.execute("SELECT content FROM question WHERE id=?", (qid,)).fetchone()[0]
        try:
            r = ai.classify(content)
            c.execute("UPDATE question SET category_l1=?,category_l2=?,knowledge_point=?,"
                      "topic_summary=? WHERE id=?",
                      (r["l1"], r["l2"], r["kp"], r["summary"], qid))
            done += 1
            if done % 10 == 0:
                db.commit()
                print(f"  重分类 {done}/{len(reclassify)} …")
        except Exception as e:
            print("  跳过", qid, str(e)[:60])
    db.commit()
    print(f"\n完成:清洗内容 {len(cleaned)} 道 | 选图残缺待确认 {len(waitconfirm)} 道 | "
          f"选图(有图)归数量 {len(forcemath)} 道 | 重分类 {done} 道")
    db.close()


if __name__ == "__main__":
    main()
