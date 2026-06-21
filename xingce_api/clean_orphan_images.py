# -*- coding: utf-8 -*-
"""清理 data/images 里的「孤儿图」——重抠/重建后不再被任何题目/材料/站点配置引用的旧图。
安全做法:先算出"在用图"集合(question_image + material_group.image_keys + 站点二维码等
设置里的图),若有任何在用图缺失(说明集合算漏了)就**拒绝删除**;否则删掉其余。
用法: python clean_orphan_images.py            # 试运行(只统计)
      python clean_orphan_images.py --apply    # 真删
"""
import os
import sys

import config
from db import SessionLocal, QuestionImage, MaterialGroup, Setting

EXT = (".png", ".jpg", ".jpeg", ".webp")


def _norm(p):
    return os.path.normcase(os.path.normpath(p))


def main():
    apply = "--apply" in sys.argv
    db = SessionLocal()
    ref = set()
    for (k,) in db.query(QuestionImage.object_key).all():
        if k:
            ref.add(_norm(k))
    for (keys,) in db.query(MaterialGroup.image_keys).all():
        for k in (keys or "").split("|"):
            if k.strip():
                ref.add(_norm(k.strip()))
    for (sval,) in db.query(Setting.sval).all():       # 站点二维码等
        sv = (sval or "").strip()
        if len(sv) < 200 and sv.lower().endswith(EXT):
            ref.add(_norm(sv))
    db.close()

    root = config.IMAGE_DIR
    refmiss = sum(1 for r in ref if not os.path.exists(os.path.join(root, r)))
    orphans, used = [], 0
    for dp, _, files in os.walk(root):
        for f in files:
            if not f.lower().endswith(EXT):
                continue
            full = os.path.join(dp, f)
            rel = _norm(os.path.relpath(full, root))
            if rel in ref:
                used += 1
            else:
                orphans.append(full)
    size = sum(os.path.getsize(p) for p in orphans) / 1024 / 1024
    print(f"在用图 {len(ref)}(缺失 {refmiss}) | 命中在用 {used} | 孤儿 "
          f"{len(orphans)} 张, {size:.1f} MB")
    if refmiss > 0:
        print("⚠ 有在用图缺失,集合可能算漏,保守起见不删。请先排查。")
        return
    if not apply:
        for p in orphans[:6]:
            print("  样例:", os.path.basename(p))
        print(f"[试运行] 加 --apply 删除这 {len(orphans)} 张")
        return
    n = 0
    for p in orphans:
        try:
            os.remove(p)
            n += 1
        except OSError:
            pass
    print(f"已删除 {n} 张孤儿图,释放约 {size:.1f} MB")


if __name__ == "__main__":
    main()
