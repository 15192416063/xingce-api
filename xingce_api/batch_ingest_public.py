# -*- coding: utf-8 -*-
r"""
批量把「行测-真题」文件夹里的所有 PDF 灌进【公共题库】(scope=public, user_id=0),
供所有用户调用。复用线上同一条入库管线(切题/抠图/资料分析/分类/向量),
所以入库时就已经分好类、打好标、建好向量——调用时直接命中,不临时计算。

幂等:同名 source_file 已存在的套卷会跳过,可中断后重复运行续灌。

用法:
    python batch_ingest_public.py                 # 灌默认的 ../行测-真题
    python batch_ingest_public.py D:\some\dir      # 指定目录
    python batch_ingest_public.py --copy           # 把 PDF 复制进 data/pdf(自包含)
"""
import os
import re
import sys
import glob
import shutil
import uuid

import config
from db import init_db, SessionLocal, IngestionJob, Paper
import ingest


def _already_done(source_file: str) -> bool:
    """该来源文件是否已有一套正常状态的公共套卷(题数>0)。"""
    db = SessionLocal()
    try:
        p = (db.query(Paper)
             .filter(Paper.scope == "public",
                     Paper.source_file == source_file,
                     Paper.status == 1,
                     Paper.question_count > 0)
             .first())
        return p is not None
    finally:
        db.close()


def ingest_one(pdf_path: str, copy: bool = False) -> dict:
    src_name = os.path.basename(pdf_path)
    if _already_done(src_name):
        return {"file": src_name, "skipped": True}

    file_path = pdf_path
    if copy:
        file_path = os.path.join(config.PDF_DIR, f"{uuid.uuid4().hex}.pdf")
        shutil.copy2(pdf_path, file_path)

    db = SessionLocal()
    job = IngestionJob(user_id=0, file_name=src_name, file_path=file_path,
                       scope="public", status=0)
    db.add(job)
    db.commit()
    jid = job.id
    db.close()

    ingest.run(jid)   # 同步执行整条管线

    db = SessionLocal()
    job = db.get(IngestionJob, jid)
    res = {"file": src_name, "job_id": jid, "status": job.status,
           "total": job.total_count, "done": job.done_count,
           "dup": job.dup_count, "graphic": job.graphic_count,
           "missing": job.missing_nums, "error": job.error_msg}
    db.close()
    return res


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    # 纯数字(年份)不当作目录名;-- 开头是选项
    args = [a for a in sys.argv[1:] if not a.startswith("--") and not a.isdigit()]
    copy = "--copy" in sys.argv
    since = None
    for i, a in enumerate(sys.argv):
        if a.startswith("--since"):
            m = re.search(r'\d{4}', a)                       # --since=2016
            if m:
                since = int(m.group())
            elif i + 1 < len(sys.argv):                      # --since 2016
                m2 = re.search(r'\d{4}', sys.argv[i + 1])
                if m2:
                    since = int(m2.group())
    src_dir = args[0] if args else os.path.join(
        os.path.dirname(config.BASE_DIR), "行测-真题")
    if not os.path.isdir(src_dir):
        print(f"[!] 目录不存在: {src_dir}")
        sys.exit(1)

    pdfs = sorted(glob.glob(os.path.join(src_dir, "*.pdf")))
    if since:   # 只灌 文件名年份 >= since 的卷(近 N 年)
        def _year(p):
            m = re.search(r'(19|20)\d{2}', os.path.basename(p))
            return int(m.group()) if m else 0
        pdfs = [p for p in pdfs if _year(p) >= since]
        print(f"年份过滤: 只灌 {since} 年及以后,共 {len(pdfs)} 份")
    if not pdfs:
        print(f"[!] {src_dir} 下没有符合条件的 PDF")
        sys.exit(1)

    init_db()
    print(f"准备灌入公共题库: {len(pdfs)} 份 PDF (来源 {src_dir})")
    if not config.EMBED_KEY:
        print("  [提醒] 未配置 XC_EMBED_KEY:题目会正常入库并分类,"
              "但向量库不会生成,「相似题/AI 推荐」要等配好 embedding key 后跑 reembed.py 才生效。")

    ok = skip = fail = 0
    for i, pdf in enumerate(pdfs, 1):
        name = os.path.basename(pdf)
        print(f"\n[{i}/{len(pdfs)}] {name}")
        try:
            r = ingest_one(pdf, copy=copy)
        except Exception as e:
            fail += 1
            print(f"    [X] 异常: {e}")
            continue
        if r.get("skipped"):
            skip += 1
            print("    [-] 已存在,跳过")
        elif r.get("status") == 3:
            ok += 1
            print(f"    [OK] 入库 {r['done']} 题 (图形 {r['graphic']} / 重复 {r['dup']})"
                  + (f" | {r['missing']}" if r.get("missing") else ""))
        else:
            fail += 1
            print(f"    [X] 失败: {r.get('error') or '状态' + str(r.get('status'))}")

    print(f"\n完成: 成功 {ok} / 跳过 {skip} / 失败 {fail}")


if __name__ == "__main__":
    main()
