# -*- coding: utf-8 -*-
"""重抠「图形推理」配图,应用页脚/水印清除修复(去掉飘进图的"第X页共Y页"、展鸿等)。
图形图当初用随机临时名抠的,无法原地覆盖,故:重抠到确定名 _gfx_{pid}_*,再按
(套卷, 卷内题号) 把题目的配图引用更新到新图。只更新匹配上的,匹配不上的保持原样(不丢图)。
纯本地、不调 API。旧的随机名图片成为孤儿(可后续清理)。
用法: python reextract_graphic_images.py
"""
import os
import sys

import fitz

import config
sys.path.insert(0, os.path.dirname(config.BASE_DIR))
import extract_graphics            # noqa: E402
import ingest_collection as ic     # noqa: E402
from db import SessionLocal, Paper, Question, QuestionImage  # noqa: E402


def main():
    doc = fitz.open(ic.COLLECTION)
    q_segs, _ = ic.collect_segments(doc)
    db = SessionLocal()
    papers = {p.source_file: p.id for p in
              db.query(Paper).filter(Paper.status == 1).all()}
    db.close()
    matched = skipped = 0
    for k in sorted(q_segs):
        name, qs0, qe0 = q_segs[k]
        pid = papers.get(name + ".pdf")
        if not pid:
            continue
        sub = fitz.open()
        sub.insert_pdf(doc, from_page=qs0 - 1, to_page=qe0 - 1)
        subpath = os.path.join(config.PDF_DIR, f"_gfx_{pid}.pdf")
        sub.save(subpath)
        sub.close()
        try:
            crops = extract_graphics.extract(subpath, config.IMAGE_DIR)
        finally:
            try:
                os.remove(subpath)
            except OSError:
                pass
        db = SessionLocal()
        n = 0
        for cr in crops:
            q = (db.query(Question)
                 .filter(Question.paper_id == pid, Question.seq_no == cr["qnum"],
                         Question.has_image == 1, Question.material_id == 0).first())
            if not q:
                skipped += 1
                continue
            rel = os.path.relpath(cr["image_path"], config.IMAGE_DIR)
            qi = (db.query(QuestionImage)
                  .filter(QuestionImage.question_id == q.id).first())
            if qi:
                qi.object_key = rel
            else:
                db.add(QuestionImage(question_id=q.id, object_key=rel, seq=0))
            matched += 1
            n += 1
        db.commit()
        db.close()
        print(f"  paper {pid} {name}: 重抠并更新 {n} 张图形图")
    print(f"完成:更新 {matched} 张,未匹配(保持原样){skipped} 张")


if __name__ == "__main__":
    main()
