# -*- coding: utf-8 -*-
"""重抠资料分析「材料组图表」图片,应用页脚/水印清除修复(去掉"第X页共Y页"、展鸿等),
原地覆盖同名 _mat_*.png(文件名确定、与 DB 引用一致),不改 DB、不调任何 API。
重建题库后若改了 extract_graphics 的遮罩规则,跑这个即可把已有图表图刷新干净。
"""
import os
import sys

import fitz

import config
sys.path.insert(0, os.path.dirname(config.BASE_DIR))   # 让 extract_material 可导入
import extract_material        # noqa: E402
import ingest_collection as ic  # noqa: E402
from db import SessionLocal, Paper  # noqa: E402


def main():
    doc = fitz.open(ic.COLLECTION)
    q_segs, _ = ic.collect_segments(doc)
    db = SessionLocal()
    papers = {p.source_file: p.id for p in
              db.query(Paper).filter(Paper.status == 1).all()}
    db.close()
    total = 0
    for k in sorted(q_segs):
        name, qs0, qe0 = q_segs[k]
        pid = papers.get(name + ".pdf")
        if not pid:
            continue
        sub = fitz.open()
        sub.insert_pdf(doc, from_page=qs0 - 1, to_page=qe0 - 1)
        subpath = os.path.join(config.PDF_DIR, f"_mat_{pid}.pdf")
        sub.save(subpath)
        sub.close()
        try:
            groups = extract_material.parse(subpath, config.IMAGE_DIR)
            n = sum(len(g["images"]) for g in groups)
            total += n
            print(f"  paper {pid} {name}: 重抠材料图表 {n} 张")
        finally:
            try:
                os.remove(subpath)
            except OSError:
                pass
    print(f"完成,共重抠材料图表 {total} 张(原地覆盖,已清页脚/水印)")


if __name__ == "__main__":
    main()
