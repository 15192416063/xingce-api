# -*- coding: utf-8 -*-
"""全国公考日历:内置基础数据 + 管理员一键自动抓取(公开页面 + AI 提取)。
内置数据给出"历年规律"兜底,抓取/手动维护负责精确日期;全部以官方公告为准。"""
import re
import json

from db import SessionLocal, ExamInfo

# 各省份(独立命题的单列;参加多省联考的标"联考")
PROVINCES_LIANKAO = [
    "安徽", "福建", "甘肃", "广西", "贵州", "海南", "河北", "河南", "黑龙江",
    "湖北", "湖南", "吉林", "江西", "辽宁", "内蒙古", "宁夏", "青海", "山西",
    "陕西", "四川", "天津", "云南", "重庆", "新疆", "西藏",
]

# 内置种子:覆盖全国。日期未知的留空,note 写历年规律。
SEEDS = [
    {"region": "全国", "exam_type": "国考",
     "name": "2027年国家公务员考试",
     "signup_start": "2026-10-15", "signup_end": "2026-10-24",
     "exam_date": "2026-11-29",
     "announce_url": "http://www.scs.gov.cn/",
     "note": "历年10月中旬报名、11月底笔试,以国家公务员局公告为准"},
    {"region": "全国", "exam_type": "事业单位",
     "name": "2026下半年事业单位联考(综合应用+职测)",
     "signup_start": "", "signup_end": "", "exam_date": "",
     "announce_url": "",
     "note": "历年9月下旬笔试,各省人事考试网发布公告"},
    {"region": "全国", "exam_type": "省考",
     "name": "2027年多省公务员联考",
     "signup_start": "", "signup_end": "", "exam_date": "",
     "announce_url": "",
     "note": "历年2月报名、3月中下旬笔试,20余省同日开考"},
    {"region": "北京", "exam_type": "省考", "name": "2027年北京市公务员考试",
     "signup_start": "", "signup_end": "", "exam_date": "",
     "announce_url": "https://rsj.beijing.gov.cn/",
     "note": "历年11月报名、12月中旬笔试(早于联考)"},
    {"region": "上海", "exam_type": "省考", "name": "2027年上海市公务员考试",
     "signup_start": "", "signup_end": "", "exam_date": "",
     "announce_url": "https://www.shacs.gov.cn/",
     "note": "历年11月报名、12月初笔试(早于联考)"},
    {"region": "江苏", "exam_type": "省考", "name": "2027年江苏省公务员考试",
     "signup_start": "", "signup_end": "", "exam_date": "",
     "announce_url": "https://jshrss.jiangsu.gov.cn/",
     "note": "历年11月报名、12月中旬笔试(早于联考)"},
    {"region": "浙江", "exam_type": "省考", "name": "2027年浙江省公务员考试",
     "signup_start": "", "signup_end": "", "exam_date": "",
     "announce_url": "https://www.zjks.com/",
     "note": "历年11-12月报名、1月上旬笔试"},
    {"region": "山东", "exam_type": "省考", "name": "2027年山东省公务员考试",
     "signup_start": "", "signup_end": "", "exam_date": "",
     "announce_url": "https://hrss.shandong.gov.cn/",
     "note": "历年11月报名、12月中旬笔试(早于联考)"},
    {"region": "广东", "exam_type": "省考", "name": "2027年广东省公务员考试",
     "signup_start": "", "signup_end": "", "exam_date": "",
     "announce_url": "https://hrss.gd.gov.cn/",
     "note": "历年1-2月报名、3月笔试,深圳市考另行单独组织"},
    {"region": "深圳", "exam_type": "市考", "name": "2027年深圳市公务员考试",
     "signup_start": "", "signup_end": "", "exam_date": "",
     "announce_url": "http://hrss.sz.gov.cn/",
     "note": "历年2月报名、3月笔试,单独命题"},
]
SEEDS += [
    {"region": p, "exam_type": "省考", "name": f"2027年{p}公务员考试(多省联考)",
     "signup_start": "", "signup_end": "", "exam_date": "",
     "announce_url": "", "note": "参加多省联考,历年2月报名、3月中下旬笔试"}
    for p in PROVINCES_LIANKAO
]

# 自动抓取的公开信息源(可按需增删;页面结构变了也不报错,只是提不出东西)
FETCH_SOURCES = [
    "http://www.scs.gov.cn/",                      # 国家公务员局
    "https://www.huatu.com/gwy/kaoshi/",           # 华图考试日历(聚合)
    "https://www.offcn.com/gjgwy/",                # 中公国考资讯(聚合)
]


def seed():
    """启动时灌内置数据(幂等:同 region+name 不重复插)。"""
    db = SessionLocal()
    try:
        have = {(e.region, e.name) for e in db.query(ExamInfo).all()}
        n = 0
        for s in SEEDS:
            if (s["region"], s["name"]) in have:
                continue
            db.add(ExamInfo(**s, origin="内置"))
            n += 1
        db.commit()
        return n
    finally:
        db.close()


def _page_text(url: str) -> str:
    import requests
    r = requests.get(url, timeout=15, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    r.raise_for_status()
    enc = r.apparent_encoding or "utf-8"
    html = r.content.decode(enc, errors="ignore")
    html = re.sub(r'(?is)<(script|style)[^>]*>.*?</\1>', ' ', html)
    text = re.sub(r'<[^>]+>', ' ', html)
    return re.sub(r'\s+', ' ', text)[:6000]


def fetch_and_update() -> dict:
    """逐个信息源抓页面 → AI 提取结构化考试信息 → 合并入库。
    返回 {fetched: 抓到条数, updated: 更新, added: 新增, errors: [...]}"""
    import ai
    fetched, updated, added, errors = 0, 0, 0, []
    db = SessionLocal()
    try:
        for url in FETCH_SOURCES:
            try:
                text = _page_text(url)
            except Exception as e:
                errors.append(f"{url}: {str(e)[:80]}")
                continue
            prompt = f"""从下面网页文字中提取**公务员/事业单位考试**的时间安排信息。
每条包含:region(省份名或"全国")、exam_type(国考/省考/市考/事业单位/选调生)、
name(考试全名)、signup_start/signup_end/exam_date(YYYY-MM-DD,不确定留空)、
announce_url(公告链接,没有留空)。没提到的不要编造。只输出JSON数组,无解释:
[{{"region":"全国","exam_type":"国考","name":"…","signup_start":"","signup_end":"","exam_date":"","announce_url":""}}]
网页文字:
{text}
JSON:"""
            try:
                resp = ai._invoke(prompt, scene="考试日历抓取")
                items = json.loads(re.search(r'\[.*\]', resp.replace("```json", "")
                                             .replace("```", ""), re.DOTALL).group(0))
            except Exception as e:
                errors.append(f"AI提取失败 {url}: {str(e)[:80]}")
                continue
            for it in items:
                name = (it.get("name") or "").strip()[:128]
                region = (it.get("region") or "").strip()[:32]
                if not name or not region:
                    continue
                fetched += 1
                row = db.query(ExamInfo).filter(ExamInfo.region == region,
                                                ExamInfo.name == name).first()
                vals = {k: (it.get(k) or "").strip()
                        for k in ("signup_start", "signup_end", "exam_date",
                                  "announce_url")}
                if row:
                    changed = False
                    for k, v in vals.items():
                        if v and getattr(row, k) != v:
                            setattr(row, k, v)
                            changed = True
                    if changed:
                        row.origin = "抓取"
                        updated += 1
                else:
                    db.add(ExamInfo(region=region,
                                    exam_type=(it.get("exam_type") or "省考")[:32],
                                    name=name, origin="抓取", **vals))
                    added += 1
            db.commit()
        return {"fetched": fetched, "updated": updated,
                "added": added, "errors": errors}
    finally:
        db.close()
