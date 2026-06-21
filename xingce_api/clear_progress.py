# -*- coding: utf-8 -*-
"""清空"做题进度"类数据,让错题本/学习数据与重建后的新题库对齐。
重建题库后题目 ID 会变(被复用),旧的做题记录/错题本会指到内容不同的新题,
故重建后建议跑一次本脚本。只清进度,**不动题目、答案、套卷、用户账号**。
打卡/访问统计(streak/VisitDay/StatDaily)按日期记,不受影响,保留。
"""
import sys

from db import (SessionLocal, PracticeRecord, WrongBook, Favorite,
                MockExam, WrongExplain)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

TABLES = (PracticeRecord, WrongBook, Favorite, MockExam, WrongExplain)


def main():
    db = SessionLocal()
    pre = {t.__name__: db.query(t).count() for t in TABLES}
    print("清空前:", pre)
    for t in TABLES:
        db.query(t).delete()
    db.commit()
    post = {t.__name__: db.query(t).count() for t in TABLES}
    db.close()
    print("清空后:", post)
    print("(题目/答案/套卷/账号/打卡统计均未改动)")


if __name__ == "__main__":
    main()
