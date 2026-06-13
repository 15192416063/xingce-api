# -*- coding: utf-8 -*-
"""
============================================================
资料分析"材料组"解析:一段共享材料/图表 + 多道小题
============================================================
难点:很多卷子没有"根据以下资料回答X-Y题"这种明确边界。
策略(事件法):在资料分析段内按阅读顺序排出"小题"和"图表"两类事件——
  · 图表紧跟在小题之后出现 → 开启新材料组(图表=新材料)
  · 段开头先出现小题(纯文字表格材料) → 也开一组
  · 后续连续小题都挂到当前材料组
每组的"材料文本 + 图表"绑给该组所有小题。
返回 [{material_text, images:[路径], subs:[{qnum, content}]}]

注意:边界靠启发式,资料分析题应入"待确认"由人工核对。
============================================================
"""
import os
import re
import fitz

import extract_graphics as eg  # 复用题号检测/真图形判定

SECTION_RE = re.compile(r'资料分析')
CHART_GAP = 50          # 同页图表竖向间隔 > 此值则视为两个材料簇
PAD = 6                 # 选项图四周留白
CHART_PAD_X = 30        # 材料图表左右留白:纵轴数值标签在图形框外,要多留
CHART_PAD_Y = 26        # 材料图表上下留白:柱顶数值/横轴类目标签也在框外


def _find_section(doc):
    """返回资料分析段起点 (page_idx, y_bottom);找不到返回 None"""
    cand = None
    for pno in range(len(doc)):
        for b in doc[pno].get_text("dict").get("blocks", []):
            if "lines" not in b:
                continue
            for ln in b["lines"]:
                txt = "".join(s["text"] for s in ln["spans"])
                if "资料分析" in txt and (
                        "部分" in txt or "所给" in txt or
                        re.search(r'[一二三四五六七八].\s*资料分析', txt)):
                    cand = (pno, ln["bbox"][3])   # 取该标题行底部
    return cand


def _chart_clusters(page, y_from, y_to):
    """页内 [y_from,y_to] 的真图形,按竖向聚簇,返回 [(y_top, rect_union), ...]
    页眉/页脚区域的对象(页码、水印、装饰)一律排除,否则会截出空白"图表"。"""
    pr = page.rect
    pw, ph = pr.width, pr.height
    top, bot = ph * eg.HEADER_RATIO, ph * eg.FOOTER_RATIO
    rects = []
    for img in page.get_images(full=True):
        try:
            for r in page.get_image_rects(img[0]):
                cy = (r.y0 + r.y1) / 2
                if y_from <= cy <= y_to and top < cy < bot \
                        and eg._is_real_figure(r, pw, ph):
                    rects.append(r)
        except Exception:
            continue
    for dr in page.get_drawings():
        r = dr.get("rect")
        if r is None:
            continue
        cy = (r.y0 + r.y1) / 2
        if y_from <= cy <= y_to and top < cy < bot and eg._is_real_figure(r, pw, ph):
            rects.append(r)
    if not rects:
        return []
    rects.sort(key=lambda r: r.y0)
    clusters, cur = [], [rects[0]]
    for r in rects[1:]:
        if r.y0 - cur[-1].y1 > CHART_GAP:
            clusters.append(cur)
            cur = [r]
        else:
            cur.append(r)
    clusters.append(cur)
    out = []
    for cl in clusters:
        x0 = min(r.x0 for r in cl); x1 = max(r.x1 for r in cl)
        y0 = min(r.y0 for r in cl); y1 = max(r.y1 for r in cl)
        out.append((y0, fitz.Rect(x0, y0, x1, y1)))
    return out


def _region_text(doc, start, end):
    (p0, y0), (p1, y1) = start, end
    parts = []
    for pno in range(p0, p1 + 1):
        page = doc[pno]; pr = page.rect
        ytop = y0 if pno == p0 else 0
        ybot = y1 if pno == p1 else pr.height
        if ybot - ytop < 2:
            continue
        parts.append(page.get_textbox(fitz.Rect(0, ytop, pr.width, ybot)))
    return "\n".join(parts).strip()


def parse(pdf_path, out_dir, zoom=3):
    os.makedirs(out_dir, exist_ok=True)
    doc = fitz.open(pdf_path)
    base = os.path.splitext(os.path.basename(pdf_path))[0]
    sec = _find_section(doc)
    if not sec:
        return []
    sp, sy = sec

    # 跨页噪声模板 + 每页噪声行缓存(渲染图表/选项图时抹掉页码/水印文字)
    try:
        _noise_set = eg._repeating_noise([doc[p].get_text() for p in range(len(doc))])
    except Exception:
        _noise_set = set()
    _noise_cache = {}

    def _page_noise(page):
        k = page.number
        if k not in _noise_cache:
            _noise_cache[k] = eg._noise_line_rects(page, _noise_set)
        return _noise_cache[k]

    # 1) 收集事件:小题 + 图表簇,按 (page,y) 阅读顺序
    events = []  # (page, y, kind, payload)
    for pno in range(sp, len(doc)):
        yf = sy if pno == sp else 0
        for (num, y, x) in eg._qnums_on_page(doc[pno]):
            if y >= yf:
                events.append((pno, y, "q", num))
        for (cy, rect) in _chart_clusters(doc[pno], yf, doc[pno].rect.height):
            events.append((pno, cy, "chart", rect))
    events.sort(key=lambda e: (e[0], e[1]))
    if not events:
        return []

    # 1.5) 过滤假题号:资料分析小题号应单调递增、步长小(剔除材料数据里的数字/重复)
    filtered, last_q = [], None
    for e in events:
        if e[2] == "q":
            num = e[3]
            if last_q is not None and not (num > last_q and num - last_q <= 8):
                continue   # 跳跃过大/不递增/重复 → 假题号
            last_q = num
        filtered.append(e)
    events = filtered

    # 2) 事件法分组(每道小题记录"下一个事件位置",用于收紧边界,防串入下一组材料)
    groups = []
    cur = None
    last_kind = None
    n = len(events)
    for i, (pno, y, kind, payload) in enumerate(events):
        nxt = (events[i+1][0], events[i+1][1]) if i + 1 < n \
            else (pno, doc[pno].rect.height - 16)
        if kind == "chart":
            if cur is None or last_kind == "q":   # 图表紧跟小题/段首 → 新组
                cur = {"material_text": "", "images": [], "subs": [],
                       "_chart_rects": []}
                groups.append(cur)
            cur["_chart_rects"].append((pno, payload))
        else:  # 小题
            if cur is None:                         # 段首先出小题 → 纯文字材料组
                cur = {"material_text": "", "images": [], "subs": [],
                       "_chart_rects": []}
                groups.append(cur)
            cur["subs"].append({"qnum": payload, "_pos": (pno, y),
                                "_next": nxt, "images": []})
        last_kind = kind

    # 3) 渲染每组图表 + 提取材料文本
    for gi, g in enumerate(groups):
        # 图表裁剪
        for (pno, rect) in g["_chart_rects"]:
            page = doc[pno]; pr = page.rect
            clip = fitz.Rect(max(0, rect.x0 - CHART_PAD_X),
                             max(pr.height * eg.HEADER_RATIO, rect.y0 - CHART_PAD_Y),
                             min(pr.width, rect.x1 + CHART_PAD_X),
                             min(pr.height * eg.FOOTER_CLIP_RATIO, rect.y1 + CHART_PAD_Y))
            try:
                mask = [r for r in _page_noise(page) if r.intersects(clip)]
                fn = os.path.join(out_dir, f"{base}_mat{gi+1}_p{pno+1}_{int(rect.y0)}.png")
                if eg.save_crop(page, clip, fn, zoom, mask_rects=mask):
                    g["images"].append(fn)
            except Exception:
                pass
        # 材料文本:本组第一个事件位置 → 第一道小题位置
        if g["subs"]:
            first_sub = g["subs"][0]["_pos"]
            mat_start = g["_chart_rects"][0] if g["_chart_rects"] else None
            if mat_start:
                start = (mat_start[0], max(0, mat_start[1].y0 - 2))
            else:
                # 纯文字材料:从上一组最后一小题之后 / 段首 到本组首题
                start = (sp, sy) if gi == 0 else groups[gi-1]["subs"][-1]["_pos"]
            g["material_text"] = _region_text(doc, start, first_sub)

    # 4) 每道小题:题干文本 + 选项图(边界收紧到下一个事件,防串入下一组材料)
    for gi, g in enumerate(groups):
        for s in g["subs"]:
            p0, y0 = s["_pos"]
            p1, y1 = s["_next"]
            s["content"] = _region_text(doc, (p0, y0), (p1, y1))
            # 选项图:小题区域内(题号行以下)的图形 = 选项是图(如"下列哪个饼图…")
            for pno in range(p0, p1 + 1):
                yf = y0 + 12 if pno == p0 else 0      # 跳过题号/题干首行
                yt = y1 if pno == p1 else doc[pno].rect.height
                for (cy, rect) in _chart_clusters(doc[pno], yf, yt):
                    page = doc[pno]; pr = page.rect
                    clip = fitz.Rect(max(0, rect.x0 - PAD),
                                     max(pr.height * eg.HEADER_RATIO, rect.y0 - PAD),
                                     min(pr.width, rect.x1 + PAD),
                                     min(pr.height * eg.FOOTER_CLIP_RATIO, rect.y1 + PAD))
                    try:
                        mask = [r for r in _page_noise(page) if r.intersects(clip)]
                        fn = os.path.join(
                            out_dir, f"{base}_opt_q{s['qnum']}_p{pno+1}_{int(rect.y0)}.png")
                        if eg.save_crop(page, clip, fn, zoom, mask_rects=mask):
                            s["images"].append(fn)
                    except Exception:
                        pass

    # 清理内部字段 + 文本降噪 + 丢空组
    footer = re.compile(r'第\s*\d+\s*页[，,]\s*共\s*\d+\s*页.*|^\s*20\d{2}年国考.*')
    clean = []
    for g in groups:
        g.pop("_chart_rects", None)
        if not g["subs"]:
            continue  # 没有小题的残留图,丢弃
        g["material_text"] = "\n".join(
            ln for ln in g["material_text"].splitlines()
            if ln.strip() and not footer.match(ln.strip())).strip()
        for s in g["subs"]:
            s.pop("_pos", None)
            s.pop("_next", None)
        clean.append(g)
    return clean


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    out = sys.argv[2] if len(sys.argv) > 2 else "material_out"
    gs = parse(sys.argv[1], out)
    print(f"共 {len(gs)} 个材料组:")
    for i, g in enumerate(gs):
        qns = [s["qnum"] for s in g["subs"]]
        print(f"\n=== 材料组{i+1}: 小题{qns} 材料图{len(g['images'])}张 ===")
        print("  材料(前80字):", re.sub(r'\s', '', g["material_text"])[:80])
        for s in g["subs"]:
            if s["images"]:
                print(f"  小题{s['qnum']} 选项图{len(s['images'])}张:",
                      [os.path.basename(x) for x in s["images"]])
