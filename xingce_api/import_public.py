# -*- coding: utf-8 -*-
r"""
在【服务器】上把公共题库种子包导入本机数据库。
- 自动重映射主键(material_group/paper/question/question_image),不会和服务器已有数据撞 id
- 按指纹去重:已存在的公共题自动跳过(可重复导入、续导)
- 拷贝抠图到本机 IMAGE_DIR(相对路径不变)
- 导入后请执行  python reembed.py  用本机 embedding 重建向量

用法:
    python import_public.py /path/to/解压目录      # 目录里含 public_seed.db 和 images/
"""
import os
import sys
import shutil
import sqlite3

import config
from db import init_db


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    replace = "--replace" in sys.argv      # 先清空本机全部公共题再导入(整库替换)
    if not args:
        raise SystemExit("用法: python import_public.py 解压目录 [--replace]")
    seed_dir = args[0]
    seed_db = os.path.join(seed_dir, "public_seed.db")
    seed_img = os.path.join(seed_dir, "images")
    if not os.path.exists(seed_db):
        raise SystemExit("目录里没有 public_seed.db: " + seed_dir)

    init_db()   # 确保本机表结构齐全
    dst_path = config.DB_URL[len("sqlite:///"):]
    db = sqlite3.connect(dst_path)
    db.execute("PRAGMA foreign_keys=OFF")
    cur = db.cursor()

    def cols(table):  # 该表除主键 id 外的列名(按本机表结构)
        return [r[1] for r in cur.execute(f'PRAGMA table_info("{table}")') if r[1] != "id"]

    if replace:   # 整库替换:删掉本机所有 public 题/卷/材料/图(不动用户私有题)
        pqids = [r[0] for r in cur.execute("SELECT id FROM question WHERE scope='public'")]
        if pqids:
            qi = ",".join(map(str, pqids))
            cur.execute(f"DELETE FROM question_image WHERE question_id IN ({qi})")
        cur.execute("DELETE FROM question WHERE scope='public'")
        cur.execute("DELETE FROM paper WHERE scope='public'")
        cur.execute("DELETE FROM material_group WHERE scope='public'")
        db.commit()
        print(f"[--replace] 已清空本机原有公共题 {len(pqids)} 道,准备重新导入")

    db.execute("ATTACH ? AS seed", (seed_db,))

    # 已有公共指纹 → 去重
    existing_fp = {r[0] for r in cur.execute(
        "SELECT fingerprint FROM question WHERE scope='public' AND fingerprint!=''")}

    # 1) material_group:逐行插入,记录 老id→新id
    mg_map = {}
    mg_cols = cols("material_group")
    for row in cur.execute(f"SELECT id,{','.join(mg_cols)} FROM seed.material_group").fetchall():
        old_id, vals = row[0], list(row[1:])
        ph = ",".join("?" * len(mg_cols))
        cur.execute(f'INSERT INTO material_group ({",".join(mg_cols)}) VALUES ({ph})', vals)
        mg_map[old_id] = cur.lastrowid

    # 2) paper:逐行插入,记录 老id→新id
    pp_map = {}
    pp_cols = cols("paper")
    for row in cur.execute(f"SELECT id,{','.join(pp_cols)} FROM seed.paper").fetchall():
        old_id, vals = row[0], list(row[1:])
        d = dict(zip(pp_cols, vals))
        d["scope"] = "public"; d["user_id"] = 0
        ph = ",".join("?" * len(pp_cols))
        cur.execute(f'INSERT INTO paper ({",".join(pp_cols)}) VALUES ({ph})',
                    [d[c] for c in pp_cols])
        pp_map[old_id] = cur.lastrowid

    # 3) question:重映射 paper_id/material_id,指纹去重,记录 老id→新id
    q_map = {}
    q_cols = cols("question")
    inserted = skipped = 0
    for row in cur.execute(f"SELECT id,{','.join(q_cols)} FROM seed.question").fetchall():
        old_id = row[0]
        d = dict(zip(q_cols, row[1:]))
        fp = d.get("fingerprint") or ""
        if fp and fp in existing_fp:
            skipped += 1
            continue
        d["scope"] = "public"
        d["paper_id"] = pp_map.get(d.get("paper_id", 0), 0)
        d["material_id"] = mg_map.get(d.get("material_id", 0), 0) if d.get("material_id") else 0
        d["vector_id"] = ""   # 由 reembed.py 重建
        ph = ",".join("?" * len(q_cols))
        cur.execute(f'INSERT INTO question ({",".join(q_cols)}) VALUES ({ph})',
                    [d[c] for c in q_cols])
        q_map[old_id] = cur.lastrowid
        if fp:
            existing_fp.add(fp)
        inserted += 1

    # 4) question_image:重映射 question_id(只导已插入的题)
    qi_cols = cols("question_image")
    n_qi = 0
    for row in cur.execute(f"SELECT id,{','.join(qi_cols)} FROM seed.question_image").fetchall():
        d = dict(zip(qi_cols, row[1:]))
        new_qid = q_map.get(d.get("question_id"))
        if not new_qid:
            continue
        d["question_id"] = new_qid
        ph = ",".join("?" * len(qi_cols))
        cur.execute(f'INSERT INTO question_image ({",".join(qi_cols)}) VALUES ({ph})',
                    [d[c] for c in qi_cols])
        n_qi += 1

    # 5) 重算每套卷题数/答案数
    for new_pid in pp_map.values():
        cur.execute("UPDATE paper SET question_count="
                    "(SELECT COUNT(*) FROM question WHERE paper_id=? AND status!=2),"
                    "answer_count="
                    "(SELECT COUNT(*) FROM question WHERE paper_id=? AND status!=2 AND answer!='')"
                    " WHERE id=?", (new_pid, new_pid, new_pid))
    db.commit()
    db.execute("DETACH seed")
    db.close()

    # 6) 拷贝抠图到本机 IMAGE_DIR(相对路径原样)
    n_img = 0
    if os.path.isdir(seed_img):
        for root, _, files in os.walk(seed_img):
            for f in files:
                sp = os.path.join(root, f)
                rel = os.path.relpath(sp, seed_img)
                dp = os.path.join(config.IMAGE_DIR, rel)
                os.makedirs(os.path.dirname(dp), exist_ok=True)
                shutil.copy2(sp, dp)
                n_img += 1

    print(f"导入完成:新增题 {inserted} 道(去重跳过 {skipped})、卷 {len(pp_map)} 套、"
          f"图 {n_img} 张。")
    print("最后一步:执行  python reembed.py  用本机 embedding 重建全部向量,检索/推荐才生效。")


if __name__ == "__main__":
    main()
