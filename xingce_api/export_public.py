# -*- coding: utf-8 -*-
r"""
导出【公共题库种子包】,用于上传 GitHub / 拷到服务器再导入。
产物(默认 seed_export/ 下,并打包成 zip):
  - public_seed.db   只含 paper/question/question_image/material_group 的 public 题数据,
                     已删除 user/login_log/setting/ai_channel 等全部敏感与无关表(无隐私)
  - images/          仅这些公共题引用到的抠图(相对路径原样保留)
不含向量库:服务器导入后跑一次 reembed.py 本地重建向量即可(免费 embedding,
           也顺带消除“两端 Chroma 版本/维度不一致”的风险)。

用法(等批量入库跑完、且服务没在写库时执行):
    python export_public.py
"""
import os
import sys
import shutil
import sqlite3
import zipfile
import datetime

import config

KEEP_TABLES = {"paper", "question", "question_image", "material_group"}


def _src_db_path():
    url = config.DB_URL
    if not url.startswith("sqlite:///"):
        raise SystemExit("仅支持 SQLite 源库导出;当前 DB_URL=" + url)
    return url[len("sqlite:///"):]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    src = _src_db_path()
    if not os.path.exists(src):
        raise SystemExit("找不到源库: " + src)

    out_dir = os.path.join(config.BASE_DIR, "seed_export")
    img_out = os.path.join(out_dir, "images")
    seed_db = os.path.join(out_dir, "public_seed.db")
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(img_out, exist_ok=True)

    # 1) 一致性快照(backup API 即使源库开着 WAL / 正在被读也能拿到完整快照)
    print("快照源库 …", src)
    seed = sqlite3.connect(seed_db)
    with sqlite3.connect(src) as srcconn:
        srcconn.backup(seed)

    cur = seed.cursor()
    # 2) 只留 public、未删的题数据
    cur.execute("DELETE FROM question WHERE NOT (scope='public' AND status!=2)")
    cur.execute("DELETE FROM paper WHERE NOT (scope='public' AND status!=2)")
    cur.execute("DELETE FROM material_group WHERE scope!='public'")
    cur.execute("DELETE FROM question_image WHERE question_id NOT IN (SELECT id FROM question)")
    # 孤儿材料组(没有任何题引用)
    cur.execute("DELETE FROM material_group WHERE id NOT IN "
                "(SELECT DISTINCT material_id FROM question WHERE material_id>0)")
    # 向量 id 清空:服务器会用自己的 embedding 重建,避免脏关联
    cur.execute("UPDATE question SET vector_id=''")
    seed.commit()

    # 3) 删掉其余所有表(user/login_log/setting/ai_channel/… 一律不带走)
    tabs = [r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for t in tabs:
        if t not in KEEP_TABLES:
            cur.execute(f'DROP TABLE IF EXISTS "{t}"')
    seed.commit()
    cur.execute("VACUUM")
    seed.commit()

    # 4) 拷贝被引用到的抠图(相对路径原样,服务器端 key 不变即可用)
    #    两处来源:题目选项图(question_image) + 资料分析材料组图表(material_group.image_keys)
    keys = set(r[0] for r in cur.execute(
        "SELECT DISTINCT object_key FROM question_image WHERE object_key!=''"))
    for (ik,) in cur.execute("SELECT image_keys FROM material_group WHERE image_keys!=''"):
        keys.update(k for k in (ik or "").split("|") if k)
    n_img = 0
    for k in keys:
        srcp = os.path.join(config.IMAGE_DIR, k)
        if os.path.exists(srcp):
            dstp = os.path.join(img_out, k)
            os.makedirs(os.path.dirname(dstp), exist_ok=True)
            shutil.copy2(srcp, dstp)
            n_img += 1

    pn = cur.execute("SELECT COUNT(*) FROM paper").fetchone()[0]
    qn = cur.execute("SELECT COUNT(*) FROM question").fetchone()[0]
    seed.close()

    # 5) 打包 zip
    stamp = datetime.datetime.now().strftime("%Y%m%d")
    zip_path = os.path.join(config.BASE_DIR, f"public_seed_{stamp}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(seed_db, "public_seed.db")
        for root, _, files in os.walk(img_out):
            for f in files:
                fp = os.path.join(root, f)
                z.write(fp, os.path.relpath(fp, out_dir))

    mb = os.path.getsize(zip_path) / 1024 / 1024
    zip_name = os.path.basename(zip_path)
    tag = f"seed-{stamp}"
    print(f"\n完成:公共卷 {pn} 套 / 题 {qn} 道 / 图 {n_img} 张")
    print(f"打包文件: {zip_path}  ({mb:.1f} MB)")
    print("\n下一步(数据走 Release,代码仍走 git,二者分开):")
    print(f"  gh release create {tag} {zip_name} -t \"公共题库种子 {stamp}\" -n \"{pn}卷/{qn}题/{n_img}图\"")
    print("  (或网页 Releases → Draft new release → 拖入该 zip → Publish)")
    print(f"\n服务器端拉取:  bash update_data.sh {tag}   (或不带 tag 取 latest)")


if __name__ == "__main__":
    main()
