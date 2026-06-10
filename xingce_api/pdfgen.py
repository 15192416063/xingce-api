# -*- coding: utf-8 -*-
"""把一组题目生成为可下载 PDF(中文用 reportlab 内置 CJK 字体,免外部字体文件)。"""
import io
import os
import re

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Image,
                                PageBreak)
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase.cidfonts import UnicodeCIDFont

import config

# 中文字体:优先嵌入真实 TTF/TTC(任何设备都能正常显示),找不到再退回内置 CID。
_FONT = "Helvetica"
_CANDIDATES = [
    os.path.join(config.BASE_DIR, "fonts", "cjk.ttf"),      # 部署时把字体放这
    os.path.join(config.BASE_DIR, "fonts", "cjk.ttc"),
    r"C:\Windows\Fonts\msyh.ttc",        # 微软雅黑(Windows)
    r"C:\Windows\Fonts\simsun.ttc",      # 宋体
    r"C:\Windows\Fonts\simhei.ttf",      # 黑体
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",  # Linux
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",  # macOS
]
for _p in _CANDIDATES:
    if os.path.exists(_p):
        try:
            if _p.lower().endswith(".ttc"):
                pdfmetrics.registerFont(TTFont("CJK", _p, subfontIndex=0))
            else:
                pdfmetrics.registerFont(TTFont("CJK", _p))
            _FONT = "CJK"
            break
        except Exception:
            continue
if _FONT == "Helvetica":
    try:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        _FONT = "STSong-Light"
    except Exception:
        pass

_TITLE = ParagraphStyle("t", fontName=_FONT, fontSize=18, leading=24,
                        spaceAfter=6, alignment=1)
_SUB = ParagraphStyle("s", fontName=_FONT, fontSize=10, leading=14,
                      textColor="#888888", alignment=1, spaceAfter=14)
_QNO = ParagraphStyle("qno", fontName=_FONT, fontSize=12, leading=18,
                      spaceBefore=10, spaceAfter=4, textColor="#1f2530")
_STEM = ParagraphStyle("stem", fontName=_FONT, fontSize=11, leading=18,
                       alignment=TA_LEFT)
_OPT = ParagraphStyle("opt", fontName=_FONT, fontSize=11, leading=17, leftIndent=10)
_MAT = ParagraphStyle("mat", fontName=_FONT, fontSize=10, leading=16,
                      textColor="#444444", backColor="#f5f1e8",
                      borderPadding=6, spaceAfter=6)
_ANS = ParagraphStyle("ans", fontName=_FONT, fontSize=11, leading=18, spaceBefore=4)
_KEY = ParagraphStyle("key", fontName=_FONT, fontSize=11, leading=20)

MAXW = 150 * mm   # 图片最大宽度


def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _parse(content):
    """拆题干+选项,返回 (stem, [(k,text),...])"""
    s = re.sub(r"^\s*\d{1,3}[\.、．]\s*", "", content or "")
    m = re.search(r"[ABCD][\.、．]", s)
    if not m:
        return s.strip(), []
    i = s.index(m.group(0))
    stem = s[:i].strip()
    opts = re.findall(r"([ABCD])[\.、．]\s*([\s\S]*?)(?=[ABCD][\.、．]|$)", s[i:])
    return stem, [(k, t.strip()) for k, t in opts]


def _img(path):
    """按最大宽度等比缩放的 Image flowable;失败返回 None"""
    try:
        if not os.path.exists(path):
            return None
        iw, ih = ImageReader(path).getSize()
        w = min(MAXW, iw)
        return Image(path, width=w, height=ih * w / iw)
    except Exception:
        return None


def build_pdf(questions, title="行测题目集", with_answer=True):
    """questions: [{seq_no,category,content,material_text,material_images,images,answer,explanation}]
    material_images/images 为绝对路径列表。返回 PDF bytes。"""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=18 * mm,
                            bottomMargin=16 * mm, leftMargin=18 * mm, rightMargin=18 * mm)
    story = [Paragraph(_esc(title), _TITLE),
             Paragraph("行测智能题库 · AI 整理 · 共 %d 题" % len(questions), _SUB)]
    keys = []
    last_mat = None
    for idx, q in enumerate(questions, 1):
        # 资料分析共享材料(同组只打印一次)
        mt = q.get("material_text") or ""
        if mt and mt != last_mat:
            story.append(Paragraph("【材料】" + _esc(mt[:1200]), _MAT))
            for ip in q.get("material_images", []):
                im = _img(ip)
                if im:
                    story.append(im)
            last_mat = mt
        stem, opts = _parse(q.get("content", ""))
        story.append(Paragraph("%d. %s" % (idx, _esc(stem)), _QNO))
        for ip in q.get("images", []):
            im = _img(ip)
            if im:
                story.append(im)
        for k, t in opts:
            story.append(Paragraph("%s. %s" % (k, _esc(t)), _OPT))
        ans = (q.get("answer") or "").strip()
        keys.append("%d.%s" % (idx, ans if ans else "—"))
        story.append(Spacer(1, 4))
    # 答案页
    if with_answer:
        story.append(PageBreak())
        story.append(Paragraph("参考答案", _TITLE))
        story.append(Paragraph("(空缺表示题库尚未录入答案)", _SUB))
        story.append(Paragraph("　".join(keys), _KEY))
    doc.build(story)
    return buf.getvalue()
