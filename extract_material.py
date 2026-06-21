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
    """返回资料分析段起点 (page_idx, y_bottom);找不到返回 None。
    兼容两种排版:① 标题与“部分/所给”同行;② “第五部分”与“资料分析”分行
    (国考真题汇编版常见),此时“资料分析”单独成行,需结合相邻行上下文判断。"""
    cand = None
    for pno in range(len(doc)):
        lines = []
        for b in doc[pno].get_text("dict").get("blocks", []):
            if "lines" not in b:
                continue
            for ln in b["lines"]:
                lines.append(("".join(s["text"] for s in ln["spans"]),
                              ln["bbox"][3]))
        for i, (txt, ybot) in enumerate(lines):
            if "资料分析" not in txt:
                continue
            prev = lines[i - 1][0] if i > 0 else ""
            nxt = lines[i + 1][0] if i + 1 < len(lines) else ""
            ctx = prev + txt + nxt
            if ("部分" in ctx or "所给" in ctx or
                    re.search(r'[一二三四五六七八].\s*资料分析', txt) or
                    txt.strip() == "资料分析" or
                    "（共" in nxt or "(共" in nxt):
                cand = (pno, ybot)   # 取该标题行底部
    return cand


_MATGRP_RE = re.compile(r'^\s*[（(][一二三四五六七八九十]+[)）]')   # 材料组标号 (一)(二)…


def _boundary_between(page, y_top, y_bot):
    """两个图形之间是否横亘着"题/材料边界行"(题号、选项、材料组标号)。
    有的话两个图分属不同题/材料,绝不能并进一张图(否则串题)。"""
    if y_bot - y_top < 6:
        return False
    try:
        d = page.get_text("dict")
    except Exception:
        return False
    for b in d.get("blocks", []):
        for ln in b.get("lines", []):
            cy = (ln["bbox"][1] + ln["bbox"][3]) / 2
            if not (y_top < cy < y_bot):
                continue
            txt = "".join(s["text"] for s in ln.get("spans", [])).strip()
            if eg._STEM_RE.match(txt) or eg._OPT_RE.match(txt) or _MATGRP_RE.match(txt):
                return True
    return False


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
        # 间距过大,或两图之间横着题/材料边界行 → 分属不同题,另起一簇
        if r.y0 - cur[-1].y1 > CHART_GAP or _boundary_between(page, cur[-1].y1, r.y0):
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


def _render_tables(doc, start, end, out_dir, base, gi, zoom):
    """检出材料区 [start,end] 内的表格(PyMuPDF find_tables),整块渲染成图片
    (上方多带一行,把表名也带进来)。返回 (图片路径列表, 表格bbox列表)。
    这样表格在前端是「一张干净的表」,而不是被竖排抽成"秋/粮/玉/米…"的乱码文字。"""
    (p0, y0), (p1, y1) = start, end
    imgs, boxes = [], []
    for pno in range(p0, min(p1 + 1, len(doc))):
        page = doc[pno]
        yt = y0 if pno == p0 else 0
        yb = y1 if pno == p1 else page.rect.height
        try:
            tables = list(page.find_tables().tables)
        except Exception:
            tables = []
        for tb in tables:
            tx0, ty0, tx1, ty1 = tb.bbox
            if tb.row_count < 2 or tb.col_count < 2:
                continue
            if not (yt - 6 <= (ty0 + ty1) / 2 <= yb + 6):
                continue
            clip = fitz.Rect(max(0, tx0 - 8), max(0, ty0 - 28),
                             min(page.rect.width, tx1 + 8),
                             min(page.rect.height, ty1 + 8))
            fn = os.path.join(out_dir, f"{base}_tab{gi+1}_p{pno+1}_{int(ty0)}.png")
            try:
                page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip).save(fn)
                imgs.append(fn)
                boxes.append((pno, fitz.Rect(tb.bbox)))
            except Exception:
                pass
    return imgs, boxes


def _region_text_excl(doc, start, end, exclude):
    """提取 [start,end] 的材料文字,但跳过 exclude 里各表格的 y 区间(表格已另存为图,
    不再把表格单元格文字重复塞进材料文本)。只留表名/说明等表外文字。"""
    (p0, y0), (p1, y1) = start, end
    ex_by_page = {}
    for pno, bb in exclude:
        ex_by_page.setdefault(pno, []).append((bb.y0, bb.y1))
    parts = []
    for pno in range(p0, min(p1 + 1, len(doc))):
        page = doc[pno]
        pr = page.rect
        ytop = y0 if pno == p0 else 0
        ybot = y1 if pno == p1 else pr.height
        cur, segs = ytop, []
        for by0, by1 in sorted(ex_by_page.get(pno, [])):
            if by0 > cur:
                segs.append((cur, min(by0, ybot)))
            cur = max(cur, by1)
        if cur < ybot:
            segs.append((cur, ybot))
        for sy0, sy1 in segs:
            if sy1 - sy0 >= 2:
                parts.append(page.get_textbox(fitz.Rect(0, sy0, pr.width, sy1)))
    return "\n".join(parts).strip()


def _strip_garble(text):
    """删掉表格被竖排抽取造成的"单字一行"乱码:连续 ≥4 行单个汉字 → 整段丢弃。
    (表格已另存为图片,这些竖排碎字只会干扰阅读。)"""
    def single(ln):
        s = ln.strip()
        return len(s) == 1 and '一' <= s <= '鿿'
    out, run = [], []
    for ln in text.splitlines():
        if single(ln):
            run.append(ln)
            continue
        if len(run) < 4:
            out.extend(run)
        run = []
        out.append(ln)
    if len(run) < 4:
        out.extend(run)
    return "\n".join(out)


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
                mask = [r for r in _page_noise(page) if r.intersects(clip)] \
                    + eg._text_mask_rects(page, clip)
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
            # 表格:整块抠成图片(替代竖排乱码文字),并把"单字碎行"乱码从材料文本里清掉
            try:
                tab_imgs, tab_boxes = _render_tables(doc, start, first_sub,
                                                     out_dir, base, gi, zoom)
                if tab_imgs:
                    g["images"].extend(tab_imgs)
                    # 表格已成图 → 材料文本跳过表格区域,再清掉残留单字碎行
                    g["material_text"] = _strip_garble(
                        _region_text_excl(doc, start, first_sub, tab_boxes))
            except Exception:
                pass

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
                        mask = [r for r in _page_noise(page) if r.intersects(clip)] \
                            + eg._text_mask_rects(page, clip)
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
