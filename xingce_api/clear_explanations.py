# -*- coding: utf-8 -*-
"""清空题库里现有的解析(第三方教辅解析,避免版权问题)。
- 保留 answer / answer_origin(答案是事实,不涉版权);
- 清空 question.explanation;
- 清空 WrongExplain 缓存(让错题讲解也按新框架重新生成)。
之后用户做错题/点击生成解析时,AI 会按「行测解析框架」按需生成,并入库全员共享。
"""
import sys

from db import SessionLocal, Question, WrongExplain

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def main():
    db = SessionLocal()
    has_exp = db.query(Question).filter(Question.explanation != "").count()
    we = db.query(WrongExplain).count()
    print(f"清空前:带解析的题 {has_exp} 道,错题讲解缓存 {we} 条")
    db.query(Question).filter(Question.explanation != "").update(
        {Question.explanation: ""}, synchronize_session=False)
    db.query(WrongExplain).delete()
    db.commit()
    left = db.query(Question).filter(Question.explanation != "").count()
    ans = db.query(Question).filter(Question.answer != "").count()
    db.close()
    print(f"清空后:带解析的题 {left} 道(应为0);有答案的题 {ans} 道(应保持不变)")


if __name__ == "__main__":
    main()
