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
FOOTER_CLIP_RATIO = 0.95  # 渲染裁剪下限:页脚"第X页,共Y页"页码区一律不进图
WATERMARK_LUMA = 195      # 灰度≥此值的像素视为水印/底纹,渲染后漂白成纯白
                          # (行测图基本是纯黑线稿,放狠些可彻底清掉浅~中灰水印)
INK_LUMA = 130            # 灰度<此值 = 真实黑色墨迹(空白检测 + 自动裁切都看它)
MIN_INK_RATIO = 0.002     # 深色像素占比低于此值 = 空白图(水印/页脚误检),丢弃
AUTOCROP_MARGIN = 6       # 自动裁切后四周保留的白边(pt)
MIN_CROP_PX = 28          # 自动裁切后宽或高 < 此(px) = 只剩页码/噪点,丢弃
FIG_TOP_PAD = 6           # 抠图从首个图形对象顶部上移这点留白(题干文字不进图)

# 页码/页眉页脚/机构水印的文字特征(精准定位后整行涂白,不靠灰度,黑字也能去)
_NOISE_TEXT = [
    re.compile(r'第\s*\d+\s*页'), re.compile(r'共\s*\d+\s*页'),
    re.compile(r'^\s*\d{1,3}\s*[/／]\s*\d{1,3}\s*$'),          # 1/25 形式页码
    re.compile(r'(微信)?公众号'), re.compile(r'祝[您你]上岸'),
    re.compile(r'上岸(小屋|小组|说|鸭|笔记|计划|成功|之路|岛|公考)'),
    re.compile(r'(中公|华图|粉笔|步知|导氮|腰果|金标尺|宏鹏|京佳)'),
    re.compile(r'[A-Za-z0-9_.-]+\.(com|cn|net|org|cc)\b'),     # 网址水印
    re.compile(r'(扫码|长按|关注)[^\n]{0,10}(领取|资料|公众号|二维码)'),
    re.compile(r'(展鸿|ZHANHONG)', re.I),                       # 展鸿教育 logo/水印
    re.compile(r'让(学习|考试)更'),                              # "让学习更快乐/让考试更简单"
]


def _repeating_noise(page_texts, ratio=0.5):
    """跨页自适应:返回"过半页面都重复出现的短行"模板(数字归一为#)。
    页眉/页脚/水印不管内容是什么,只要每页重复就被找出来——换任何卷子都通用。"""
    from collections import Counter
    pages = [t for t in page_texts if t]
    if len(pages) < 3:
        return set()
    cnt = Counter()
    for t in pages:
        for ln in {l.strip() for l in t.splitlines()}:
            if 2 <= len(ln) <= 25:
                cnt[re.sub(r'\d+', '#', ln)] += 1
    thresh = max(3, int(len(pages) * ratio))
    return {k for k, v in cnt.items() if v >= thresh}


def _noise_line_rects(page, noise_set=None):
    """该页"噪声文字行"的包围盒(只取页眉/页脚区,绝不碰正文图形区,以免抹掉图)。
    命中规则:整行匹配跨页重复模板,或命中页码/水印文字特征。"""
    ph = page.rect.height
    hi, lo = ph * HEADER_RATIO, ph * FOOTER_RATIO
    out = []
    try:
        d = page.get_text("dict")
    except Exception:
        return out
    for b in d.get("blocks", []):
        for ln in b.get("lines", []):
            txt = "".join(s["text"] for s in ln.get("spans", [])).strip()
            if not txt:
                continue
            # 明确的页码/机构水印文字特征(第X页/共Y页/展鸿/让学习更/网址…)→ 不论在页面
            # 哪个位置都涂白:这些绝不会是行测正文,放在正文带里也照清(汇编版页码常飘进图)
            if any(p.search(txt) for p in _NOISE_TEXT):
                out.append(fitz.Rect(ln["bbox"]))
                continue
            cy = (ln["bbox"][1] + ln["bbox"][3]) / 2
            if hi < cy < lo:                 # 跨页重复模板仅在页眉/页脚带清,避免误伤正文
                continue
            norm = re.sub(r'\d+', '#', txt)
            if noise_set and norm in noise_set:
                out.append(fitz.Rect(ln["bbox"]))
    return out


# 题干 / 文字型选项 / 成句散文 的特征(这些都不该进图,统一涂白)
_STEM_RE = re.compile(r'^\s*\d{1,3}\s*[.、．]')          # 行首题号 = 题干
_OPT_RE = re.compile(r'^\s*[A-DＡ-Ｄ]\s*[.、．]')          # 行首 A./B./C./D. = 选项
_MATLBL_RE = re.compile(r'^\s*[（(][一二三四五六七八九十]+[)）]\s*$')  # 独占行的材料组标号(二)
_CJK_RE = re.compile(r'[一-鿿]')


# 资料分析/材料段的"分界行":图形题带不能越过它,否则会把下游材料的图(饼图等)并进来
_SECTION_RE = [
    re.compile(r'根据.{0,14}(图|表|资料|材料|数据|统计|文字|信息).{0,10}(回答|完成|作答)'),
    re.compile(r'资料分析'),
    re.compile(r'^\s*[（(][一二三四五六七八九十]+[)）]'),       # 材料组标号 (一)(二)
    re.compile(r'^\s*[一二三四五六七八九十]\s*[、.]\s*(资料|给定资料)'),
]


def _section_cut_y(page, y0, y1):
    """带内若出现"资料分析/材料"分界行,返回其 y(用于截断图形题带,挡住下游材料图)。"""
    try:
        d = page.get_text("dict")
    except Exception:
        return None
    best = None
    for b in d.get("blocks", []):
        for ln in b.get("lines", []):
            ly = ln["bbox"][1]
            if not (y0 < ly < y1):
                continue
            txt = "".join(s["text"] for s in ln.get("spans", [])).strip()
            if any(p.search(txt) for p in _SECTION_RE):
                if best is None or ly < best:
                    best = ly
    return best


def _text_mask_rects(page, clip):
    """clip 内"应涂白的文字行"包围盒:题干、文字型选项(A.xxx 含汉字/数字)、成句散文。
    保留:坐标轴/数值标签、图内短标签、以及图形推理的 A B C D 单字母选项标号、A.A 序号。
    思路:把图周围的文字噪声抹掉,再交给 autocrop 贴紧真正的图形墨迹。"""
    out = []
    try:
        d = page.get_text("dict")
    except Exception:
        return out
    cy0, cy1 = clip.y0, clip.y1
    for b in d.get("blocks", []):
        for ln in b.get("lines", []):
            y0, y1 = ln["bbox"][1], ln["bbox"][3]
            if y1 <= cy0 or y0 >= cy1:          # 与 clip 纵向无交叠 → 跳过
                continue
            txt = "".join(s["text"] for s in ln.get("spans", [])).strip()
            if not txt:
                continue
            body = txt.replace(" ", "")
            tail = txt.lstrip()[2:]             # 选项字母+分隔符之后的内容
            mask = False
            if _STEM_RE.match(txt) or _MATLBL_RE.match(txt):          # 题干 / 独占行材料组标号
                mask = True
            elif _OPT_RE.match(txt) and (_CJK_RE.search(tail) or any(c.isdigit() for c in tail)):
                mask = True                                           # 文字型选项 A.含汉字/数字
            elif ("。" in txt) or ("？" in txt) or ("：" in txt and len(body) >= 8):
                mask = True                                           # 成句/材料散文
            elif "，" in txt and len(body) >= 12:
                mask = True                                           # 长的逗号句(散文)
            if mask:
                out.append(fitz.Rect(ln["bbox"]))
    return out


def save_crop(page, clip, fn, zoom=ZOOM, mask_rects=None):
    """渲染并保存裁剪图:抹除页码/水印文字 + 漂白底纹 + 自动裁到内容包围盒 + 空白检测。
    - mask_rects: 页眉/页脚噪声文字行(PDF点坐标),命中后整行涂白(黑字页码也能去)
    - 浅~中灰水印/底纹一律漂白成纯白
    - 自动裁掉四周空白,贴着真实内容(精度↑,顺带切掉残留的页眉/页脚/侧边水印)
    返回 True=已保存;False=内容近乎空白(误检的水印/页码区),不保存。"""
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip)
    try:
        import numpy as np
        from PIL import Image
        arr = np.frombuffer(pix.samples, dtype=np.uint8) \
            .reshape(pix.height, pix.width, pix.n)[:, :, :3].copy()
        # ① 精准抹除页码/页眉页脚/机构水印文字行(整行涂白,与颜色深浅无关)
        if mask_rects:
            for r in mask_rects:
                ix0 = int(max(0, (r.x0 - clip.x0) * zoom))
                ix1 = int(min(arr.shape[1], (r.x1 - clip.x0) * zoom))
                iy0 = int(max(0, (r.y0 - clip.y0) * zoom))
                iy1 = int(min(arr.shape[0], (r.y1 - clip.y0) * zoom))
                if ix1 > ix0 and iy1 > iy0:
                    arr[iy0:iy1, ix0:ix1] = 255
        gray = arr.mean(axis=2)
        arr[gray >= WATERMARK_LUMA] = 255      # ② 漂白"祝您上岸"之类浅灰水印/底纹
        ink = arr.mean(axis=2) < INK_LUMA      # 抹除+漂白后剩下的才是真墨迹
        if float(ink.mean()) < MIN_INK_RATIO:
            return False                       # 几乎没有深色内容 → 空白误检
        ys, xs = np.where(ink)                 # ③ 自动裁切到内容包围盒
        m = int(AUTOCROP_MARGIN * zoom)
        y0 = max(0, int(ys.min()) - m); y1 = min(arr.shape[0], int(ys.max()) + 1 + m)
        x0 = max(0, int(xs.min()) - m); x1 = min(arr.shape[1], int(xs.max()) + 1 + m)
        out = arr[y0:y1, x0:x1]
        if out.shape[0] < MIN_CROP_PX or out.shape[1] < MIN_CROP_PX:
            return False                       # 裁完只剩一行页码/零碎噪点
        Image.fromarray(out).save(fn)
        return True
    except Exception:
        pix.save(fn)   # 后处理不可用就存原图,绝不因此丢题
        return True


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
    """该 y 区间内是否含"真图形"对象,返回 (有无, 最大对象面积, 图形并集包围盒)。
    并集包围盒用于把裁剪上沿压到首个图形顶部 → 题干文字不再进图。"""
    ph, pw = page.rect.height, page.rect.width
    top, bot = ph * HEADER_RATIO, ph * FOOTER_RATIO
    max_area = 0.0
    uy0, uy1 = 1e9, -1e9
    ux0, ux1 = 1e9, -1e9

    def _acc(r):
        nonlocal max_area, uy0, uy1, ux0, ux1
        max_area = max(max_area, abs((r.x1 - r.x0) * (r.y1 - r.y0)))
        uy0, uy1 = min(uy0, r.y0), max(uy1, r.y1)
        ux0, ux1 = min(ux0, r.x0), max(ux1, r.x1)

    # 内嵌位图
    try:
        for img in page.get_images(full=True):
            try:
                for r in page.get_image_rects(img[0]):
                    cy = (r.y0 + r.y1) / 2
                    if y0 <= cy <= y1 and top < cy < bot and _is_real_figure(r, pw, ph):
                        _acc(r)
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
                _acc(r)
    except Exception:
        pass
    union = fitz.Rect(ux0, uy0, ux1, uy1) if max_area > 0 else None
    return max_area > 0, max_area, union


def extract(pdf_path, out_dir="graphic_crops", zoom=ZOOM):
    """主入口:抠出 PDF 里所有含图题的裁剪图,返回清单"""
    os.makedirs(out_dir, exist_ok=True)
    doc = fitz.open(pdf_path)
    base = os.path.splitext(os.path.basename(pdf_path))[0]
    results = []
    # 跨页噪声模板(页眉/页脚/水印重复行),全卷算一次
    try:
        noise_set = _repeating_noise([doc[p].get_text() for p in range(len(doc))])
    except Exception:
        noise_set = set()

    for pno in range(len(doc)):
        page = doc[pno]
        pr = page.rect
        qs = _qnums_on_page(page)
        if not qs:
            continue
        page_mask = _noise_line_rects(page, noise_set)   # 本页页眉/页脚噪声行(算一次)
        for i, (num, y, x) in enumerate(qs):
            y0 = max(0, y - 4)
            y1 = qs[i + 1][1] - 4 if i + 1 < len(qs) else pr.height - 16
            y1 = min(y1, pr.height * FOOTER_CLIP_RATIO)   # 不把页脚页码切进图
            cut = _section_cut_y(page, y0, y1)            # 遇资料分析/材料分界 → 截断
            if cut and cut - y0 >= MIN_BAND_H:
                y1 = cut
            if y1 - y0 < MIN_BAND_H:
                continue
            has_visual, area, union = _visual_objects_in(page, y0, y1)
            if not has_visual:
                continue  # 纯文字题,不需要裁图

            # 自检:区域底部是否贴着页底(可能跨页) → 低置信
            near_bottom = y1 > pr.height * 0.92 and i + 1 >= len(qs)
            confidence = "low" if near_bottom else "high"
            reason = "可能跨页,建议人工确认" if near_bottom else "区域内确含图形对象"

            try:
                # 题干不入图:裁剪上沿压到首个图形顶部(题干在图外已有文字,不重复)
                crop_y0 = y0
                if union is not None:
                    crop_y0 = min(max(y0, union.y0 - FIG_TOP_PAD), y1 - MIN_BAND_H)
                clip = fitz.Rect(18, crop_y0, pr.width - 18, y1)
                mask = [r for r in page_mask if r.intersects(clip)]
                # 图形顶部以上一律涂白:挡掉残留的题干文字行(autocrop 才不会把它带回来)
                if union is not None and union.y0 > clip.y0:
                    mask.append(fitz.Rect(clip.x0, clip.y0, clip.x1, union.y0))
                # 题干/文字型选项/散文一律涂白,只留图形墨迹
                mask += _text_mask_rects(page, clip)
                fn = os.path.join(out_dir, f"{base}_p{pno+1}_q{num}.png")
                if not save_crop(page, clip, fn, zoom, mask_rects=mask):
                    continue   # 区域里只有水印/底纹,不是真图形题
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
    import sys
    # Windows 控制台默认 cp936,直接 print 中文/符号会 UnicodeEncodeError;统一切到 UTF-8
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if len(sys.argv) < 2:
        print("用法: python extract_graphics.py 卷子.pdf [输出目录]")
        sys.exit(1)
    out = sys.argv[2] if len(sys.argv) > 2 else "graphic_crops"
    res = extract(sys.argv[1], out)
    hi = sum(1 for r in res if r["confidence"] == "high")
    lo = len(res) - hi
    print(f"共抠出含图题 {len(res)} 道 (高置信 {hi} / 待确认 {lo}),输出在 {out}/")
    for r in res:
        flag = "[OK]" if r["confidence"] == "high" else "[?]"
        print(f" {flag} 第{r['page']}页 题{r['qnum']}: {r['reason']}")
