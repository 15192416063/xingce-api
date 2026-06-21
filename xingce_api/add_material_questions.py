# -*- coding: utf-8 -*-
"""增量补「资料分析」材料组+小题:不动已入库的文字题/答案,只把之前因排版被漏掉的
资料分析补进现有套卷,匹配答案,最后批量重建向量。

依赖:已修复的 extract_material._find_section(兼容「第五部分/资料分析」分行排版)。
配对沿用 ingest_collection.collect_segments 的 (年份,卷种) → 页码段。
"""
import os
import sys
import time
import shutil

import fitz

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import config
import ai
import textutil
from db import (SessionLocal, Question, QuestionImage, MaterialGroup, Paper)
import ingest as ingest_mod
import ingest_collection as ic
import extract_material
import rebuild_vectors_batch

LOG = os.path.join(config.BASE_DIR, "_add_material.log")


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def add_for_paper(doc, pid, source, q_start, q_end, a_start, a_end):
    """对一套卷:抽题目段子PDF→解析资料分析组→落库→抽答案段匹配。返回(组数,小题数,答案数)。"""
    # 1) 题目段子 PDF
    sub = fitz.open()
    sub.insert_pdf(doc, from_page=q_start - 1, to_page=q_end - 1)
    subpath = os.path.join(config.PDF_DIR, f"_mat_{pid}.pdf")
    sub.save(subpath)
    sub.close()
    try:
        subdoc = fitz.open(subpath)
        try:
            doc_noise = textutil.detect_repeating_noise(
                [p.get_text() for p in subdoc])
        except Exception:
            doc_noise = set()
        subdoc.close()

        groups = extract_material.parse(subpath, config.IMAGE_DIR)
    finally:
        try:
            os.remove(subpath)
        except OSError:
            pass

    # 已有指纹(去重,含此前入库的全部题)
    db = SessionLocal()
    existing = {fp for (fp,) in db.query(Question.fingerprint)
                .filter(Question.scope == "public").all() if fp}
    db.close()

    n_grp = n_sub = 0
    for g in groups:
        mat_imgs = [os.path.relpath(p, config.IMAGE_DIR) for p in g["images"]]
        db = SessionLocal()
        mg = MaterialGroup(job_id=0, scope="public", source=source,
                           material_text=textutil.clean_text(g["material_text"], doc_noise),
                           image_keys="|".join(mat_imgs))
        db.add(mg)
        db.commit()
        mid = mg.id
        db.close()
        n_grp += 1

        for s in g["subs"]:
            body = s.get("content", "")
            fp = ingest_mod._fingerprint(f"DA|{source}|{s['qnum']}|{body}")
            if fp in existing:
                continue
            existing.add(fp)
            l1, l2, summary, diff, kp = "资料分析", "资料分析", "资料分析", 2, ""
            if config.DEEPSEEK_API_KEY and body:
                try:
                    c = ai.classify(f"【材料】{g['material_text'][:400]}\n【小题】{body}")
                    l1, l2 = c["l1"], c["l2"]
                    summary, diff, kp = c["summary"], c["diff"], c["kp"]
                except Exception:
                    pass
            opt_imgs = [os.path.relpath(p, config.IMAGE_DIR)
                        for p in s.get("images", [])]
            db = SessionLocal()
            qe = Question(job_id=0, paper_id=pid, scope="public", source=source,
                          seq_no=s["qnum"], material_id=mid,
                          category_l1=l1, category_l2=l2, knowledge_point=kp,
                          topic_summary=summary, difficulty=diff,
                          content=textutil.clean_text(body, doc_noise),
                          fingerprint=fp,
                          has_image=1 if (mat_imgs or opt_imgs) else 0,
                          confidence=60, status=1)
            db.add(qe)
            db.commit()
            qid = qe.id
            for k, rel in enumerate(opt_imgs):
                db.add(QuestionImage(question_id=qid, object_key=rel, seq=k))
            db.commit()
            db.close()
            n_sub += 1

    # 2) 答案段:解析「题号→答案/解析」,回填本卷尚无答案的题(主要是新增资料分析)
    atext = "\n".join(doc[p].get_text() for p in range(a_start - 1, a_end))
    key = ai.parse_answer_key(atext)
    filled = 0
    if key:
        db = SessionLocal()
        qs = db.query(Question).filter(Question.paper_id == pid,
                                       Question.status == 1,
                                       Question.answer == "").all()
        for q in qs:
            n = q.seq_no or 0
            it = key.get(n)
            if not it:
                continue
            q.answer = it["answer"]
            q.answer_origin = "official"
            if it.get("explanation"):
                q.explanation = it["explanation"][:4000]
            filled += 1
        db.commit()
        db.close()

    # 3) 回填套卷题数/答案数
    db = SessionLocal()
    p = db.get(Paper, pid)
    p.question_count = db.query(Question).filter(
        Question.paper_id == pid, Question.status == 1).count()
    p.answer_count = db.query(Question).filter(
        Question.paper_id == pid, Question.status == 1,
        Question.answer != "").count()
    db.commit()
    qc, ac = p.question_count, p.answer_count
    db.close()
    log(f"  组+{n_grp} 资料分析小题+{n_sub} 新填答案+{filled} | 本卷现 题{qc}/答{ac}")
    return n_grp, n_sub, filled


def main():
    open(LOG, "w", encoding="utf-8").close()
    log("=== 增量补资料分析 开始 ===")
    doc = fitz.open(ic.COLLECTION)
    q_segs, a_segs = ic.collect_segments(doc)

    db = SessionLocal()
    papers = {p.source_file: p.id for p in
              db.query(Paper).filter(Paper.status == 1).all()}
    db.close()

    tot_grp = tot_sub = tot_fill = 0
    for k in sorted(q_segs):
        name, qs0, qe0 = q_segs[k]
        ans = a_segs.get(k)
        src = name + ".pdf"
        pid = papers.get(src)
        if not pid:
            log(f"  ⚠ 找不到套卷 source={src},跳过")
            continue
        if not ans:
            log(f"  ⚠ {name} 缺答案段,跳过")
            continue
        log(f"==== {name} (paper={pid}) 题目p{qs0}-{qe0} 答案p{ans[1]}-{ans[2]} ====")
        g, s, f = add_for_paper(doc, pid, src, qs0, qe0, ans[1], ans[2])
        tot_grp += g
        tot_sub += s
        tot_fill += f

    log(f"资料分析补充小计: 材料组 {tot_grp} / 小题 {tot_sub} / 回填答案 {tot_fill}")

    # 批量重建向量(含新增资料分析)
    log("开始批量重建向量 ...")
    rebuild_vectors_batch.main()

    db = SessionLocal()
    tq = db.query(Question).filter(Question.status != 2).count()
    ta = db.query(Question).filter(Question.status != 2,
                                   Question.answer != "").count()
    tm = db.query(MaterialGroup).count()
    db.close()
    log(f"=== 完成 题库共 题{tq}/有答案{ta}/材料组{tm} ===")


if __name__ == "__main__":
    main()
