# -*- coding: utf-8 -*-
"""
============================================================
行测 PDF 图形题/图表题 稳健抠图模块
============================================================
原理:题号 y 坐标锚定 + 区域内图形对象自检 + clip 渲染整带
  - 不让 VLM 猜坐标,坐标全来自 PDF 文本流(精确)
  - "哪道题区域里有图形对象,就给哪道题裁图"——自检即检测
  - 题号格式覆盖 . / ． / 、 三种(实测 46 份真题全覆盖)
  - 损坏 PDF 容错:单图读取失败跳过,不让整份崩
  - 自检兜底:裁出的区域必须含真实图形对象,否则标低置信交人工确认

返回每道含图题:{page, qnum, y0, y1, image_path, confidence, reason}
confidence: high=区域内确含图形对象  low=需人工确认
============================================================
"""
import fitz
import re
import os

# 题号:span 开头是 数字 + . ． 、 之一(题号可能独占span,也可能与题干同span),且在左边距
QNUM_RE = re.compile(r'^(\d{1,3})[\.．、]')
ZOOM = 3                  # 渲染倍率
LEFT_MARGIN_RATIO = 0.15  # 题号 x 必须在页宽前 15%
MIN_BAND_H = 30           # 太矮的带跳过
MIN_IMG_AREA = 2500       # 小于此面积(渲染前pt²)的对象忽略
MIN_OBJ_H = 35            # 关键:真图形高度≥35pt;文字行渲染成的矢量条高仅~15pt,据此剔除
MAX_W_RATIO = 0.95        # 全宽(>95%页宽)且矮的对象多为文字行/分隔线,排除
HEADER_RATIO = 0.06       # 顶部 6% 视为页眉,其图忽略
FOOTER_RATIO = 0.94       # 底部 6% 视为页脚


def _qnums_on_page(page):
    """返回该页题号 [(num, y_top, x0), ...] 按 y 排序,带几何校验
    在"行"层面检测:把一行所有span拼成整行文本再判行首,
    兼容题号与分隔符/内容被拆成多个span的情况(如山东事业编)。"""
    pw = page.rect.width
    res = []
    try:
        d = page.get_text("dict")
    except Exception:
        return res
    for block in d.get("blocks", []):
        if "lines" not in block:
            continue
        for line in block["lines"]:
            spans = line.get("spans", [])
            if not spans:
                continue
            line_text = "".join(s["text"] for s in spans).strip()
            x0 = line["bbox"][0]
            y0 = line["bbox"][1]
            m = QNUM_RE.match(line_text)
            if m and x0 < pw * LEFT_MARGIN_RATIO:
                res.append((int(m.group(1)), y0, x0))
    res.sort(key=lambda r: r[1])
    return res


def _is_real_figure(r, pw, ph):
    """判断一个对象矩形是否像"真图形"(而非文字行条/分隔线/整页背景/装饰)"""
    w, h = r.x1 - r.x0, r.y1 - r.y0
    a = abs(w * h)
    if a < MIN_IMG_AREA:
        return False
    if h < MIN_OBJ_H:           # 太矮 = 文字行渲染成的矢量条
        return False
    if w > pw * MAX_W_RATIO and h < MIN_OBJ_H * 2:  # 全宽且矮 = 分隔线/整行
        return False
    if r.y0 < -5 or r.y1 > ph + 5:   # 超出页面边界 = 整页背景框/边框
        return False
    if h > ph * 0.8:                 # 比页面还高 = 背景,真图形不会这么高
        return False
    return True


def _visual_objects_in(page, y0, y1):
    """该 y 区间内是否含"真图形"对象,返回 (有无, 最大对象面积)"""
    ph, pw = page.rect.height, page.rect.width
    top, bot = ph * HEADER_RATIO, ph * FOOTER_RATIO
    max_area = 0.0
    # 内嵌位图
    try:
        for img in page.get_images(full=True):
            try:
                for r in page.get_image_rects(img[0]):
                    cy = (r.y0 + r.y1) / 2
                    if y0 <= cy <= y1 and top < cy < bot and _is_real_figure(r, pw, ph):
                        max_area = max(max_area, abs((r.x1 - r.x0) * (r.y1 - r.y0)))
            except Exception:
                continue  # 损坏对象跳过
    except Exception:
        pass
    # 矢量绘制
    try:
        for dr in page.get_drawings():
            r = dr.get("rect")
            if r is None:
                continue
            cy = (r.y0 + r.y1) / 2
            if y0 <= cy <= y1 and top < cy < bot and _is_real_figure(r, pw, ph):
                max_area = max(max_area, abs((r.x1 - r.x0) * (r.y1 - r.y0)))
    except Exception:
        pass
    return max_area > 0, max_area


def extract(pdf_path, out_dir="graphic_crops", zoom=ZOOM):
    """主入口:抠出 PDF 里所有含图题的裁剪图,返回清单"""
    os.makedirs(out_dir, exist_ok=True)
    doc = fitz.open(pdf_path)
    base = os.path.splitext(os.path.basename(pdf_path))[0]
    results = []

    for pno in range(len(doc)):
        page = doc[pno]
        pr = page.rect
        qs = _qnums_on_page(page)
        if not qs:
            continue
        for i, (num, y, x) in enumerate(qs):
            y0 = max(0, y - 4)
            y1 = qs[i + 1][1] - 4 if i + 1 < len(qs) else pr.height - 16
            if y1 - y0 < MIN_BAND_H:
                continue
            has_visual, area = _visual_objects_in(page, y0, y1)
            if not has_visual:
                continue  # 纯文字题,不需要裁图

            # 自检:区域底部是否贴着页底(可能跨页) → 低置信
            near_bottom = y1 > pr.height * 0.92 and i + 1 >= len(qs)
            confidence = "low" if near_bottom else "high"
            reason = "可能跨页,建议人工确认" if near_bottom else "区域内确含图形对象"

            try:
                clip = fitz.Rect(18, y0, pr.width - 18, y1)
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip)
                fn = os.path.join(out_dir, f"{base}_p{pno+1}_q{num}.png")
                pix.save(fn)
            except Exception as e:
                results.append({"page": pno + 1, "qnum": num, "y0": round(y0),
                                "y1": round(y1), "image_path": None,
                                "confidence": "low", "reason": f"渲染失败:{e}"})
                continue

            results.append({"page": pno + 1, "qnum": num, "y0": round(y0),
                            "y1": round(y1), "image_path": fn,
                            "confidence": confidence, "reason": reason})
    return results


if __name__ == "__main__":
    import sys, json
    if len(sys.argv) < 2:
        print("用法: python extract_graphics.py 卷子.pdf [输出目录]")
        sys.exit(1)
    out = sys.argv[2] if len(sys.argv) > 2 else "graphic_crops"
    res = extract(sys.argv[1], out)
    hi = sum(1 for r in res if r["confidence"] == "high")
    lo = len(res) - hi
    print(f"共抠出含图题 {len(res)} 道 (高置信 {hi} / 待确认 {lo}),输出在 {out}/")
    for r in res:
        flag = "  " if r["confidence"] == "high" else "⚠️"
        print(f" {flag} 第{r['page']}页 题{r['qnum']}: {r['reason']}")
