# -*- coding: utf-8 -*-
r"""
批量给【公共题库】各套卷灌答案。复用线上同一套解析(ai.parse_answer_key):
紧凑排版"1-5 ABCDA""1.A 2.B"本地正则解析(免 token);带【答案】+解析的长文档才调 LLM。
答案标 origin=official(覆盖 AI 猜的答案)。灌完本地库后重新导出种子、部署到服务器即可。

答案目录里每个文件 = 一套卷:
  - 文件名(去扩展名)需能对上某套卷(包含年份+卷型关键词即可,如 "2024 地市级")
  - 支持 .txt / .md(直接读)和 .pdf(自动提取文字)
  - 内容示例:
      1-5 ABCDA  6-10 BCDAB ...
      或  1.A 2.B 3.C ...
      或  1.【答案】A 解析:……(带解析也能解析出来)

用法:
    python batch_answers.py 答案目录
    python batch_answers.py 答案目录 --dry   # 只匹配、不写库(先核对对应关系)
"""
import os
import re
import sys
import glob

import config
import ai
from db import SessionLocal, Paper, Question


def _norm(s: str) -> str:
    return re.sub(r'[\s《》()()【】.,、_:：年-]', '', s or '').lower()


def _read_text(path: str) -> str:
    if path.lower().endswith(".pdf"):
        try:
            import fitz
            return "\n".join(p.get_text() for p in fitz.open(path))
        except Exception as e:
            print("   读取 PDF 失败:", e)
            return ""
    for enc in ("utf-8", "gbk"):
        try:
            with open(path, encoding=enc) as f:
                return f.read()
        except Exception:
            continue
    return ""


def _qnum(q) -> int:
    if q.seq_no:
        return q.seq_no
    m = re.match(r'\s*(\d{1,3})', q.content or "")
    return int(m.group(1)) if m else 0


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry" in sys.argv
    if not args:
        print("用法: python batch_answers.py 答案目录 [--dry]")
        sys.exit(1)
    folder = args[0]
    files = sorted(f for f in glob.glob(os.path.join(folder, "*"))
                   if os.path.isfile(f) and f.lower().endswith((".txt", ".md", ".pdf")))
    if not files:
        print("目录里没有 .txt/.md/.pdf 答案文件:", folder)
        sys.exit(1)

    db = SessionLocal()
    papers = db.query(Paper).filter(Paper.scope == "public", Paper.status == 1).all()
    used = set()
    print(f"公共卷 {len(papers)} 套,答案文件 {len(files)} 个\n")
    for f in files:
        stem = os.path.splitext(os.path.basename(f))[0]
        ns = _norm(stem)
        match = None
        for p in papers:
            if p.id in used:
                continue
            ps = _norm(os.path.splitext(p.source_file or "")[0] + (p.title or ""))
            if ns and (ns in ps or ps in ns):
                match = p
                break
        if not match:
            print(f"[!] 未匹配到卷: {stem}  —— 请让文件名包含 年份+卷型(如 2024地市级)")
            continue
        used.add(match.id)
        if dry:
            print(f"[对应] {stem}  ->  {match.title[:30]}")
            continue
        text = _read_text(f)
        if len(text.strip()) < 4:
            print(f"[!] {stem}: 内容为空,跳过")
            continue
        key = ai.parse_answer_key(text[:60000])
        if not key:
            print(f"[!] {match.title[:24]}: 没解析出「题号→答案」,检查答案格式")
            continue
        qs = db.query(Question).filter(Question.paper_id == match.id,
                                       Question.status != 2).all()
        n = 0
        for q in qs:
            it = key.get(_qnum(q))
            if not it:
                continue
            q.answer = it["answer"]
            q.answer_origin = "official"
            if it.get("explanation"):
                q.explanation = it["explanation"][:4000]
            n += 1
        match.answer_count = sum(1 for q in qs if (q.answer or "").strip())
        db.commit()
        print(f"[OK] {match.title[:28]}: 解析 {len(key)} 题,回填 {n} 题")
    db.close()
    if not dry:
        print("\n完成。下一步:导出新种子并部署:")
        print("  python export_public.py  → 传 Release → 服务器 update_data.sh")


if __name__ == "__main__":
    main()
