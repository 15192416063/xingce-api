# -*- coding: utf-8 -*-
"""题干/材料文本清洗:去页脚、页眉、水印、漂移题号、材料序号等抠取噪声。
核心是"自适应"——水印/页眉/页脚的本质是"几乎每页都重复的短文本",
靠跨页频率自动识别(不依赖关键词),所以换任何来源的卷子都通用。
入库时和API返回时都调用(幂等)。"""
import re
from collections import Counter

_NOISE = [
    re.compile(r'第\s*\d+\s*页\s*[，,]?\s*共\s*\d+\s*页'),       # 页脚页码
    re.compile(r'共\s*\d+\s*页|第\s*\d+\s*页'),                  # 单独页码
    re.compile(r'20\d{2}\s*年\s*国(考|家公务员)[^\n]*'),         # 试卷页眉
    re.compile(r'(微信)?公众号[:：]?[^\n]*'),                    # 公众号水印
    re.compile(r'[局灯]{0,3}祝[您你]上岸[^\n]*'),                # "祝您上岸"水印
    re.compile(r'上岸(小屋|小组|说|鸭|笔记|计划|成功|之路|岛|公考)[^\n]*'),  # "上岸XX"机构号
    re.compile(r'(中公|华图|粉笔|步知|导氮|腰果|阿甘|金标尺|宏鹏|京佳|李梦娇|花生十三)[^\n]{0,8}(教育|公考|网校|课堂)?'),  # 常见培训机构
    re.compile(r'[A-Za-z0-9_.-]+\.(com|cn|net|org|cc)\S*'),     # 网址
    re.compile(r'(扫码|长按|关注)[^\n]{0,10}(领取|资料|公众号|二维码)[^\n]*'),  # 引流
]


def detect_repeating_noise(page_texts) -> set:
    """自适应噪声检测:返回"在过半页面重复出现的短行"模板(数字归一为#)。
    水印/页眉/页脚不管内容是什么,只要每页重复,就会被找出来——无需关键词。"""
    pages = [t for t in page_texts if t]
    n = len(pages)
    if n < 3:
        return set()
    cnt = Counter()
    for t in pages:
        seen = set()
        for line in t.splitlines():
            ln = line.strip()
            if not (2 <= len(ln) <= 25):     # 只看短行(正文段落不会重复)
                continue
            norm = re.sub(r'\d+', '#', ln)   # 数字归一,使"第1页"="第2页"
            if norm not in seen:
                seen.add(norm)
                cnt[norm] += 1
    thresh = max(3, int(n * 0.5))            # 出现在过半页面 → 判为噪声
    return {k for k, v in cnt.items() if v >= thresh}


def clean_text(s: str, extra_noise: set = None) -> str:
    if not s:
        return s or ""
    # 自适应噪声:整行匹配重复模板的删掉
    if extra_noise:
        kept = []
        for line in s.splitlines():
            norm = re.sub(r'\d+', '#', line.strip())
            if norm and norm in extra_noise:
                continue
            kept.append(line)
        s = "\n".join(kept)
    for p in _NOISE:
        s = p.sub("", s)
    # 独占一行的漂移题号(如 "115.")
    s = re.sub(r'(?m)^\s*\d{1,3}[\.、．]\s*$', '', s)
    # 独占一行的材料组序号(如 "(二)")
    s = re.sub(r'(?m)^\s*[（(][一二三四五六七八九十][)）]\s*$', '', s)
    # 折叠多余空行
    s = re.sub(r'\n{2,}', '\n', s)
    return s.strip()
