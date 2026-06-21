# -*- coding: utf-8 -*-
"""把「真题合集.pdf」切分成多套卷,题目+答案一并入库(DB + 向量库)。

合集版面(已探明):
  - 前半(p4~307):各套「行政职业能力测验」卷 + 申论卷,按年份交替
  - 后半(p309~550):各套「行测」参考答案及解析 + 申论答案
我们只取「行测」卷(9 套:2022~2024 × 副省级/省级、地市级、行政执法),
按 (年份, 卷种) 把题目段与答案段配对。申论是主观题,跳过。

复用现成、已验证的管线:
  - 每套卷切出独立子 PDF → 建 IngestionJob → ingest.run() 切题/抠图/资料分析组/落库/向量
  - 答案段文本 → ai.parse_answer_key() 解析「题号→答案/解析」→ 按卷内题号回填
  - 最后做一遍稳健的向量重建(带重试),确保每道题都进了向量库

用法:
  python ingest_collection.py --clear   # 先清空旧题库再全量入库(默认)
  python ingest_collection.py --no-clear # 不清空,仅追加
"""
import os
import re
import sys
import time
import shutil
import tempfile

import fitz

# Windows 控制台默认 GBK,Chinese/特殊符号(«»⚠)直接 print 会 UnicodeEncodeError。
# 统一把标准输出改成 UTF-8 且坏字符不报错(本脚本另有独立 UTF-8 日志文件)。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import config
import ai
import vectors
from db import (SessionLocal, IngestionJob, Question, QuestionImage,
                MaterialGroup, Paper)
import ingest as ingest_mod

COLLECTION = os.path.join(config.BASE_DIR, "..", "行测-真题", "真题合集.pdf")
LOG = os.path.join(config.BASE_DIR, "_ingest_collection.log")

# 卷种(长名优先,避免「省级综合管理类」被「省级」截断)
JUAN_RE = re.compile(
    r'(副省级|省级综合管理类|地市级综合管理类|地市级|省级|行政执法类|行政执法)')
YEAR_RE = re.compile(r'(20\d{2})\s*年')


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def seg_title(doc, start_page):
    """从某套卷/答案的首页文本提炼 (年份, 卷种),拼成统一卷名。"""
    txt = ""
    for p in range(start_page - 1, min(start_page + 1, len(doc))):
        txt += doc[p].get_text()
    y = YEAR_RE.search(txt)
    j = JUAN_RE.search(txt)
    year = y.group(1) if y else "?"
    juan = j.group(1) if j else "?"
    return year, juan, f"{year}年国家公务员考试《行测》（{juan}）"


def collect_segments(doc):
    """按书签大纲切出 行测「题目段」与「答案段」,各自按 (年份,卷种) 建索引。"""
    toc = doc.get_toc()
    l1 = [(t, p) for (lvl, t, p) in toc if lvl == 1]
    q_segs, a_segs = {}, {}        # (year,juan) -> (title, start, end)
    for i, (title, start) in enumerate(l1):
        end = (l1[i + 1][1] - 1) if i + 1 < len(l1) else len(doc)
        # 注意:书签标题被截断为「《行政职业能」(缺「力测验》"),故用短串匹配
        is_q = "行政职业能" in title                      # 题目卷
        is_a = ("行测" in title) and ("行政职业能" not in title)  # 答案段
        if not (is_q or is_a):
            continue                                         # 申论/目录,跳过
        year, juan, name = seg_title(doc, start)
        rec = (name, start, end)
        (q_segs if is_q else a_segs)[(year, juan)] = rec
    return q_segs, a_segs


def make_subpdf(doc, start, end, tag):
    """抽取 [start,end](1-based,含端点)为独立子 PDF,返回临时文件路径。"""
    sub = fitz.open()
    sub.insert_pdf(doc, from_page=start - 1, to_page=end - 1)
    fd, path = tempfile.mkstemp(prefix=f"paper_{tag}_", suffix=".pdf",
                                dir=config.PDF_DIR)
    os.close(fd)
    sub.save(path)
    sub.close()
    return path


def clear_all():
    """清空旧题库:向量库 + 题/卷/材料组/图/入库任务表。用户数据(做题记录等)保留。"""
    db = SessionLocal()
    pre = {
        "question": db.query(Question).count(),
        "paper": db.query(Paper).count(),
        "material_group": db.query(MaterialGroup).count(),
        "question_image": db.query(QuestionImage).count(),
        "ingestion_job": db.query(IngestionJob).count(),
    }
    log(f"清空前计数: {pre}")
    for model in (QuestionImage, Question, MaterialGroup, Paper, IngestionJob):
        db.query(model).delete()
    db.commit()
    db.close()
    # 向量库:整目录删掉重建,杜绝旧/孤立 collection 残留
    vectors._collection.cache_clear()
    if os.path.isdir(config.VECTOR_DIR):
        shutil.rmtree(config.VECTOR_DIR, ignore_errors=True)
    os.makedirs(config.VECTOR_DIR, exist_ok=True)
    log("已清空:DB 题库表 + 向量库目录")


def ingest_one(name, subpdf):
    """对一套卷子 PDF 走完整入库管线,返回 (paper_id, job_id)。"""
    db = SessionLocal()
    job = IngestionJob(user_id=0, file_name=name + ".pdf", file_path=subpdf,
                       scope="public", status=0)
    db.add(job)
    db.commit()
    job_id = job.id
    db.close()
    ingest_mod.run(job_id)                # 同步执行:切题/抠图/资料组/落库/向量
    db = SessionLocal()
    job = db.get(IngestionJob, job_id)
    paper = (db.query(Paper)
             .filter(Paper.source_file == name + ".pdf", Paper.scope == "public")
             .order_by(Paper.id.desc()).first())
    pid = paper.id if paper else 0
    log(f"  入库完成 job={job_id} status={job.status} "
        f"total={job.total_count} done={job.done_count} dup={job.dup_count} "
        f"missing=[{job.missing_nums}] err=[{job.error_msg}] paper_id={pid}")
    db.close()
    return pid, job_id


def _qnum_of(content):
    m = re.match(r'\s*(\d{1,3})\s*[\.、．]', content or "")
    return int(m.group(1)) if m else 0


def apply_answers(doc, pid, a_start, a_end):
    """从答案段抽文本,解析「题号→答案/解析」,按卷内题号回填到该套卷的题。"""
    text = "\n".join(doc[p].get_text() for p in range(a_start - 1, a_end))
    key = ai.parse_answer_key(text)
    if not key:
        log(f"  ⚠ 答案段未解析出任何题号→答案 (p{a_start}-{a_end})")
        return 0, 0
    db = SessionLocal()
    qs = db.query(Question).filter(Question.paper_id == pid,
                                   Question.status == 1).all()
    matched = 0
    for q in qs:
        n = q.seq_no or _qnum_of(q.content)
        it = key.get(n)
        if not it:
            continue
        q.answer = it["answer"]
        q.answer_origin = "official"
        if it.get("explanation"):
            q.explanation = it["explanation"][:4000]
        matched += 1
    db.flush()
    p = db.get(Paper, pid)
    p.answer_count = db.query(Question).filter(
        Question.paper_id == pid, Question.status == 1,
        Question.answer != "").count()
    db.commit()
    ac = p.answer_count
    db.close()
    log(f"  答案:解析 {len(key)} 题,匹配回填 {matched} 题,本卷有答案 {ac} 题")
    return len(key), matched


def rebuild_vectors():
    """稳健向量重建:确保每道有考点摘要的题都进向量库(带重试,失败跳过不中断)。"""
    db = SessionLocal()
    qs = db.query(Question).filter(Question.topic_summary != "",
                                   Question.status != 2).all()
    db.close()
    log(f"向量重建:待处理 {len(qs)} 题")
    ok = fail = 0
    for i, q in enumerate(qs, 1):
        for attempt in range(3):
            try:
                vid = vectors.upsert(q.id, q.topic_summary, q.scope,
                                     q.category_l2)
                db = SessionLocal()
                row = db.get(Question, q.id)
                row.vector_id = vid
                db.commit()
                db.close()
                ok += 1
                break
            except Exception as e:
                if attempt == 2:
                    fail += 1
                    log(f"  向量失败 q={q.id}: {str(e)[:120]}")
                else:
                    time.sleep(1.5)
        if i % 50 == 0:
            log(f"  向量进度 {i}/{len(qs)} (ok={ok} fail={fail})")
    log(f"向量重建完成:成功 {ok},失败 {fail}")
    return ok, fail


def main():
    do_clear = "--no-clear" not in sys.argv
    open(LOG, "w", encoding="utf-8").close()
    log(f"=== 真题合集入库开始 clear={do_clear} ===")
    if not os.path.exists(COLLECTION):
        log("找不到合集 PDF: " + COLLECTION)
        sys.exit(1)

    doc = fitz.open(COLLECTION)
    q_segs, a_segs = collect_segments(doc)
    keys = sorted(q_segs.keys())
    log(f"识别到行测题目卷 {len(q_segs)} 套,答案段 {len(a_segs)} 套")
    for k in keys:
        qn, qs0, qe0 = q_segs[k]
        ans = a_segs.get(k)
        log(f"  {k}: 题目 p{qs0}-{qe0} | 答案 "
            f"{'p%d-%d' % (ans[1], ans[2]) if ans else '缺失'}  «{qn}»")

    if do_clear:
        clear_all()

    summary = []
    for k in keys:
        name, qs0, qe0 = q_segs[k]
        log(f"==== 入库:{name}  (题目 p{qs0}-{qe0}) ====")
        sub = make_subpdf(doc, qs0, qe0, f"{k[0]}_{k[1]}")
        try:
            pid, job_id = ingest_one(name, sub)
        finally:
            try:
                os.remove(sub)
            except OSError:
                pass
        parsed = matched = 0
        ans = a_segs.get(k)
        if pid and ans:
            parsed, matched = apply_answers(doc, pid, ans[1], ans[2])
        summary.append((name, pid, parsed, matched))

    # 全量向量重建(同进程内 embedding 已缓存,基本是命中,几乎不额外花钱)
    vok, vfail = rebuild_vectors()

    # 收尾汇总
    db = SessionLocal()
    tot_q = db.query(Question).filter(Question.status != 2).count()
    tot_ans = db.query(Question).filter(Question.status != 2,
                                        Question.answer != "").count()
    tot_paper = db.query(Paper).filter(Paper.status == 1).count()
    db.close()
    vcount = vectors._collection().count()
    log("==== 全部完成 ====")
    for name, pid, parsed, matched in summary:
        log(f"  {name}: paper={pid} 答案解析={parsed} 回填={matched}")
    log(f"题库总计: 题 {tot_q} / 有答案 {tot_ans} / 套卷 {tot_paper} / "
        f"向量 {vcount} (重建 ok={vok} fail={vfail})")


if __name__ == "__main__":
    main()
