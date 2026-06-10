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
from db import SessionLocal, IngestionJob, Question, QuestionImage, MaterialGroup

# 复用已验证的抠图/材料组模块(在上级目录)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import extract_graphics  # noqa: E402
import extract_material  # noqa: E402


def _fingerprint(text: str) -> str:
    return hashlib.md5(re.sub(r'\s', '', text)[:80].encode("utf-8")).hexdigest()


def _full_text(pdf_path: str) -> str:
    doc = fitz.open(pdf_path)
    parts = [p.get_text() for p in doc]
    full = "\n".join(parts)
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
    pdf_path, scope, source = job.file_path, job.scope, job.file_name
    db.close()

    try:
        _set(job_id, status=1, progress=2)  # 解析中

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

        # 4) 文字题:切题(需 DeepSeek)。失败则降级。资料分析段已从全文截掉。
        text_qs, split_err = [], ""
        try:
            full = _full_text(pdf_path)
            text_qs = ai.split_questions(
                full, progress_cb=lambda p: _set(job_id, progress=2 + int(p * 23)))
            text_qs = [q for q in text_qs
                       if q.get("type") not in ("图形推理", "资料分析")]
        except Exception as e:
            split_err = f"文字题切题跳过({e});已入图形/资料分析题。"

        total = len(text_qs) + graphic_count + da_sub_count
        _set(job_id, status=2, total_count=total, graphic_count=graphic_count)

        done = dup = 0

        # ---- 文字题入库 ----
        for q in text_qs:
            body = q.get("content", "")
            fp = _fingerprint(body)
            done += 1
            if fp in existing:
                dup += 1
                _set(job_id, done_count=done, progress=25 + int(done * 75 / max(total, 1)))
                continue
            existing.add(fp)
            c = ai.classify(body)
            db = SessionLocal()
            qe = Question(job_id=job_id, scope=scope, source=source,
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
            qe = Question(job_id=job_id, scope=scope, source=source, seq_no=cr["qnum"],
                          category_l1=l1, category_l2=l2, topic_summary=summary,
                          difficulty=diff, content=textutil.clean_text(body, doc_noise),
                          fingerprint=fp,
                          has_image=1, confidence=conf, status=0)  # 待确认
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
                qe = Question(job_id=job_id, scope=scope, source=source,
                              seq_no=s["qnum"], material_id=mid,
                              category_l1=l1, category_l2=l2, knowledge_point=kp,
                              topic_summary=summary, difficulty=diff,
                              content=textutil.clean_text(body, doc_noise),
                              fingerprint=fp,
                              has_image=1 if (mat_imgs or opt_imgs) else 0,
                              confidence=60, status=0)  # 待确认
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

        _set(job_id, status=3, progress=100, done_count=done, dup_count=dup,
             error_msg=split_err)

    except Exception as e:
        _set(job_id, status=4, error_msg=str(e)[:1000])
