# -*- coding: utf-8 -*-
"""入库编排:PDF → 文字题切题分类 + 图形题抠图 → 去重 → 落库 + 向量。
图形题入库为 status=0 待确认(人工核对裁剪图后才进出题池)。"""
import os
import re
import sys
import shutil
import hashlib

import fitz  # PyMuPDF

import config
import ai
import vectors
import textutil
from db import SessionLocal, IngestionJob, Question, QuestionImage, MaterialGroup, Paper

# 复用已验证的抠图/材料组模块(在上级目录)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import extract_graphics  # noqa: E402
import extract_material  # noqa: E402


def _fingerprint(text: str) -> str:
    return hashlib.md5(re.sub(r'\s', '', text)[:80].encode("utf-8")).hexdigest()


def _full_text(pdf_path: str) -> str:
    """整卷文字。优先转 Markdown(版面/换行/表格更干净,切题准确率明显更高),
    转换失败再退回纯文本提取。"""
    full = ""
    try:
        import pymupdf4llm
        full = pymupdf4llm.to_markdown(pdf_path, show_progress=False)
    except Exception:
        pass
    if len(re.sub(r'\s', '', full)) < 50:   # MD 转出来近乎空 → 退回纯文本
        doc = fitz.open(pdf_path)
        full = "\n".join(p.get_text() for p in doc)
        if len(re.sub(r'\s', '', full)) < 50:
            # 还是空:扫描件(无文字层)→ 视觉模型整页 OCR(需配置视觉渠道)
            full = _ocr_pdf(pdf_path)
    # 砍掉申论/材料写作尾部
    for marker in ["材料处理题", "申论"]:
        idx = full.find(marker)
        if idx > len(full) * 0.5:
            full = full[:idx]
            break
    # 砍掉资料分析段(它走材料组那条线,不进文字切题)
    idx = full.rfind("资料分析")
    if idx > len(full) * 0.4:
        full = full[:idx]
    return full


def _ocr_pdf(pdf_path: str, max_pages: int = 30) -> str:
    """扫描版 PDF:逐页渲染成图,视觉模型转写成 Markdown。"""
    doc = fitz.open(pdf_path)
    if len(doc) > max_pages:
        raise RuntimeError(f"扫描版PDF页数过多({len(doc)}页,上限{max_pages}页),"
                           "请拆分后分批上传")
    parts = []
    for page in doc:
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))   # 2倍分辨率,识别更准
        parts.append(ai.ocr_page(pix.tobytes("png")))
    return "\n\n".join(parts)


def _band_text(pdf_path, page_no, y0, y1) -> str:
    """取某页某y区间的文字,作图形题的题干"""
    try:
        page = fitz.open(pdf_path)[page_no - 1]
        clip = fitz.Rect(0, y0, page.rect.width, y1)
        return page.get_textbox(clip).strip()
    except Exception:
        return ""


def _set(job_id, **kw):
    db = SessionLocal()
    try:
        job = db.get(IngestionJob, job_id)
        for k, v in kw.items():
            setattr(job, k, v)
        db.commit()
    finally:
        db.close()


def run(job_id: int):
    """后台执行入库。异常时把任务标失败。"""
    db = SessionLocal()
    job = db.get(IngestionJob, job_id)
    pdf_path, scope, source, uid = job.file_path, job.scope, job.file_name, job.user_id
    db.close()
    # 用户私有库:无管理员审核环节,抠图/资料分析题直接入池
    auto_ok = scope.startswith("user:")
    paper_id = 0

    try:
        _set(job_id, status=1, progress=2)  # 解析中

        # 0) 一份 PDF = 一套卷(题库以套卷为单位展示)
        db = SessionLocal()
        paper = Paper(user_id=uid, scope=scope,
                      title=(source or "未命名卷").rsplit(".", 1)[0][:255],
                      source_file=source or "")
        db.add(paper)
        db.commit()
        paper_id = paper.id
        db.close()

        # 0) 自适应噪声检测:扫全部页,找出"过半页面重复的短行"(页眉/页脚/水印)
        try:
            _doc = fitz.open(pdf_path)
            doc_noise = textutil.detect_repeating_noise([p.get_text() for p in _doc])
        except Exception:
            doc_noise = set()

        # 1) 已有指纹(去重)
        db = SessionLocal()
        existing = {fp for (fp,) in db.query(Question.fingerprint)
                    .filter(Question.scope == scope).all() if fp}
        db.close()

        # 2) 资料分析:材料组解析(纯本地)。每组=共享材料+图表,挂多道小题。
        material_groups = extract_material.parse(pdf_path, config.IMAGE_DIR)
        da_qnums = {s["qnum"] for g in material_groups for s in g["subs"]}
        da_sub_count = sum(len(g["subs"]) for g in material_groups)

        # 3) 图形题:抠图(纯本地)。排除资料分析小题(它们走材料组那条线)。
        crops = [c for c in extract_graphics.extract(pdf_path, config.IMAGE_DIR)
                 if c["qnum"] not in da_qnums]
        graphic_count = len(crops)

        # 4) 文字题:切题(需 LLM)。失败则降级。资料分析段已从全文截掉。
        text_qs, split_err, full = [], "", ""
        try:
            full = _full_text(pdf_path)
            text_qs = ai.split_questions(
                full, progress_cb=lambda p: _set(job_id, progress=2 + int(p * 20)))
            text_qs = [q for q in text_qs
                       if q.get("type") not in ("图形推理", "资料分析")]
        except Exception as e:
            split_err = f"文字题切题跳过({e});已入图形/资料分析题。"

        # 4b) 缺号校验闭环:全文题号锚点 vs 实际切出的题号,缺的定点重切一次
        missing_report = ""
        try:
            if text_qs and not split_err:
                anchors = ai.detect_qnums(full)
                got = ({int(q.get("qnum") or 0) for q in text_qs}
                       | {c["qnum"] for c in crops} | da_qnums)
                pos_sorted = sorted(anchors.items(), key=lambda kv: kv[1])
                nxt = {n: (pos_sorted[i + 1][1] if i + 1 < len(pos_sorted)
                           else len(full))
                       for i, (n, _) in enumerate(pos_sorted)}

                def _is_q(n):   # 锚点段里有选项标记才算题(答案区的"1.A"不算)
                    return re.search(r'[ABD][\.、．]', full[anchors[n]:nxt[n]][:2500])

                holes = [n for n in sorted(anchors) if n not in got and _is_q(n)]
                if holes:
                    _set(job_id, progress=23)
                    slices = [full[anchors[n]:nxt[n]][:2500] for n in holes[:20]]
                    rescued = [q for q in ai.split_questions("\n\n".join(slices))
                               if q.get("type") not in ("图形推理", "资料分析")]
                    text_qs.extend(rescued)
                    got |= {int(q.get("qnum") or 0) for q in rescued}
                still = [n for n in sorted(anchors) if n not in got and _is_q(n)]
                if still:
                    missing_report = "缺:" + ",".join(map(str, still[:40]))
        except Exception:
            pass

        total = len(text_qs) + graphic_count + da_sub_count
        _set(job_id, status=2, total_count=total, graphic_count=graphic_count)

        done = dup = 0

        # ---- 文字题入库:先去重,再批量分类(10题/次,省一半以上LLM调用),最后落库 ----
        uniq = []
        for q in text_qs:
            body = q.get("content", "")
            fp = _fingerprint(body)
            if fp in existing:
                done += 1
                dup += 1
                _set(job_id, done_count=done, dup_count=dup,
                     progress=25 + int(done * 75 / max(total, 1)))
                continue
            existing.add(fp)
            uniq.append((body, fp, int(q.get("qnum") or 0)))
        cls_list = []
        for i in range(0, len(uniq), 10):
            cls_list.extend(ai.classify_batch([b for b, _, _ in uniq[i:i + 10]]))
            _set(job_id, progress=25 + int((done + min(i + 10, len(uniq)) * 0.5)
                                           * 75 / max(total, 1)))
        for (body, fp, qnum), c in zip(uniq, cls_list):
            done += 1
            db = SessionLocal()
            qe = Question(job_id=job_id, paper_id=paper_id, scope=scope, source=source,
                          seq_no=qnum,
                          category_l1=c["l1"], category_l2=c["l2"],
                          knowledge_point=c["kp"], topic_summary=c["summary"],
                          difficulty=c["diff"], content=textutil.clean_text(body, doc_noise),
                          fingerprint=fp, has_image=0, confidence=100, status=1)
            db.add(qe)
            db.commit()
            qid = qe.id
            db.close()
            vid = vectors.upsert(qid, c["summary"], scope, c["l2"])
            db = SessionLocal()
            db.get(Question, qid).vector_id = vid
            db.commit()
            db.close()
            _set(job_id, done_count=done, dup_count=dup,
                 progress=25 + int(done * 75 / max(total, 1)))

        # ---- 图形题入库(status=0 待确认) ----
        for cr in crops:
            done += 1
            band = _band_text(pdf_path, cr["page"], cr["y0"], cr["y1"])
            body = band or f"图形推理题(第{cr['page']}页 第{cr['qnum']}题,见图)"
            fp = _fingerprint(f"GRAPHIC|{source}|{cr['page']}|{cr['qnum']}|{body}")
            if fp in existing:
                dup += 1
                _set(job_id, done_count=done, progress=25 + int(done * 75 / max(total, 1)))
                continue
            existing.add(fp)
            # 图形题分类:有题干就让AI判,否则默认图形推理
            l1, l2, summary, diff = "判断推理", "图形推理", "图形推理", 2
            if config.DEEPSEEK_API_KEY and band:
                try:
                    c = ai.classify(body)
                    l1, l2, summary, diff = c["l1"], c["l2"], c["summary"], c["diff"]
                except Exception:
                    pass
            conf = 70 if cr["confidence"] == "high" else 40
            db = SessionLocal()
            qe = Question(job_id=job_id, paper_id=paper_id, scope=scope,
                          source=source, seq_no=cr["qnum"],
                          category_l1=l1, category_l2=l2, topic_summary=summary,
                          difficulty=diff, content=textutil.clean_text(body, doc_noise),
                          fingerprint=fp,
                          has_image=1, confidence=conf,
                          status=1 if auto_ok else 0)  # 公共库待确认,私库直接入池
            db.add(qe)
            db.commit()
            qid = qe.id
            # 绑定图(存相对路径)
            rel = os.path.relpath(cr["image_path"], config.IMAGE_DIR)
            db.add(QuestionImage(question_id=qid, object_key=rel, seq=0))
            db.commit()
            db.close()
            try:
                vid = vectors.upsert(qid, summary, scope, l2)
                db = SessionLocal()
                db.get(Question, qid).vector_id = vid
                db.commit()
                db.close()
            except Exception:
                pass
            _set(job_id, done_count=done, dup_count=dup,
                 progress=25 + int(done * 75 / max(total, 1)))

        # ---- 资料分析材料组入库(每组共享材料+图表,所有小题绑定;status=0 待确认) ----
        for g in material_groups:
            # 材料组图片(相对路径,竖线分隔)
            mat_imgs = [os.path.relpath(p, config.IMAGE_DIR) for p in g["images"]]
            db = SessionLocal()
            mg = MaterialGroup(job_id=job_id, scope=scope, source=source,
                               material_text=textutil.clean_text(g["material_text"], doc_noise),
                               image_keys="|".join(mat_imgs))
            db.add(mg)
            db.commit()
            mid = mg.id
            db.close()

            for s in g["subs"]:
                done += 1
                body = s.get("content", "")
                fp = _fingerprint(f"DA|{source}|{s['qnum']}|{body}")
                if fp in existing:
                    dup += 1
                    _set(job_id, done_count=done,
                         progress=25 + int(done * 75 / max(total, 1)))
                    continue
                existing.add(fp)
                l1, l2, summary, diff, kp = "资料分析", "资料分析", "资料分析", 2, ""
                if config.DEEPSEEK_API_KEY and body:
                    try:
                        # 带材料背景分类,考点更准
                        c = ai.classify(f"【材料】{g['material_text'][:400]}\n【小题】{body}")
                        l1, l2 = c["l1"], c["l2"]
                        summary, diff, kp = c["summary"], c["diff"], c["kp"]
                    except Exception:
                        pass
                # 选项图(选项本身是图,如"下列哪个饼图…")
                opt_imgs = [os.path.relpath(p, config.IMAGE_DIR)
                            for p in s.get("images", [])]
                db = SessionLocal()
                qe = Question(job_id=job_id, paper_id=paper_id, scope=scope, source=source,
                              seq_no=s["qnum"], material_id=mid,
                              category_l1=l1, category_l2=l2, knowledge_point=kp,
                              topic_summary=summary, difficulty=diff,
                              content=textutil.clean_text(body, doc_noise),
                              fingerprint=fp,
                              has_image=1 if (mat_imgs or opt_imgs) else 0,
                              confidence=60,
                              status=1 if auto_ok else 0)  # 公共库待确认,私库直接入池
                db.add(qe)
                db.commit()
                qid = qe.id
                for k, rel in enumerate(opt_imgs):    # 选项图绑到本小题
                    db.add(QuestionImage(question_id=qid, object_key=rel, seq=k))
                db.commit()
                db.close()
                try:
                    vid = vectors.upsert(qid, summary, scope, l2)
                    db = SessionLocal()
                    db.get(Question, qid).vector_id = vid
                    db.commit()
                    db.close()
                except Exception:
                    pass
                _set(job_id, done_count=done, dup_count=dup,
                     progress=25 + int(done * 75 / max(total, 1)))

        # 套卷收尾:回填题数;一道题都没入的空卷直接标删
        db = SessionLocal()
        p = db.get(Paper, paper_id)
        n = db.query(Question).filter(Question.paper_id == paper_id,
                                      Question.status != 2).count()
        p.question_count = n
        if n == 0:
            p.status = 2
        db.commit()
        db.close()

        _set(job_id, status=3, progress=100, done_count=done, dup_count=dup,
             missing_nums=missing_report, error_msg=split_err)

    except Exception as e:
        _set(job_id, status=4, error_msg=str(e)[:1000])
        if paper_id:
            try:
                db = SessionLocal()
                p = db.get(Paper, paper_id)
                if p and not db.query(Question).filter(
                        Question.paper_id == paper_id).count():
                    p.status = 2
                    db.commit()
                db.close()
            except Exception:
                pass
