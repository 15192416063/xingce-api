# -*- coding: utf-8 -*-
"""重抠资料分析「材料组」:重新解析每套卷的材料,把表格整块抠成图片(find_tables),
清掉竖排乱码 + 题型说明前缀,然后**原地更新现有材料组**的 material_text 与 image_keys
(按套卷内顺序一一对应;组数不一致则跳过该卷保平安)。纯本地、不调 API。
用法: python reextract_material.py
"""
import os
import sys

import fitz

import config
sys.path.insert(0, os.path.dirname(config.BASE_DIR))
import extract_material            # noqa: E402
import ingest_collection as ic     # noqa: E402
import textutil                    # noqa: E402
import fix_text_quality            # noqa: E402
from db import SessionLocal, Paper, MaterialGroup  # noqa: E402


def main():
    doc = fitz.open(ic.COLLECTION)
    q_segs, _ = ic.collect_segments(doc)
    db = SessionLocal()
    papers = {p.source_file: p.id for p in
              db.query(Paper).filter(Paper.status == 1).all()}
    db.close()
    tot_grp = tot_tab = 0
    for k in sorted(q_segs):
        name, qs0, qe0 = q_segs[k]
        src = name + ".pdf"
        if src not in papers:
            continue
        pid = papers[src]
        sub = fitz.open()
        sub.insert_pdf(doc, from_page=qs0 - 1, to_page=qe0 - 1)
        subpath = os.path.join(config.PDF_DIR, f"_mat_{pid}.pdf")
        sub.save(subpath)
        sub.close()
        try:
            groups = extract_material.parse(subpath, config.IMAGE_DIR)
        finally:
            try:
                os.remove(subpath)
            except OSError:
                pass
        db = SessionLocal()
        existing = (db.query(MaterialGroup).filter(MaterialGroup.source == src)
                    .order_by(MaterialGroup.id).all())
        if len(existing) != len(groups):
            print(f"  [跳过] {name}: 材料组数不一致 旧{len(existing)} 新{len(groups)}")
            db.close()
            continue
        ntab = 0
        for eg_row, ng in zip(existing, groups):
            imgs = [os.path.relpath(p, config.IMAGE_DIR) for p in ng["images"]]
            eg_row.image_keys = "|".join(imgs)
            mt = textutil.clean_text(ng["material_text"])
            mt = fix_text_quality.fix_material(mt)
            eg_row.material_text = mt
            ntab += sum(1 for p in imgs if "_tab" in p)
        db.commit()
        db.close()
        tot_grp += len(groups)
        tot_tab += ntab
        print(f"  {name}: 更新 {len(groups)} 组,其中表格图 {ntab} 张")
    print(f"完成:更新 {tot_grp} 个材料组,新增表格图 {tot_tab} 张")


if __name__ == "__main__":
    main()
