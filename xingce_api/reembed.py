# -*- coding: utf-8 -*-
"""换 embedding 模型后,重建向量库(旧向量维度不同,必须重算)。
用法:配好 .env 里的 XC_EMBED_KEY 后,运行  python reembed.py
"""
import chromadb
import config
import vectors
from db import SessionLocal, Question

print("使用 embedding:", config.EMBED_MODEL, "@", config.EMBED_BASE_URL)
if not config.EMBED_KEY:
    raise SystemExit("请先在 .env 里设置 XC_EMBED_KEY")

# 1) 删除旧 collection(维度变了,必须重建)
client = chromadb.PersistentClient(path=config.VECTOR_DIR)
try:
    client.delete_collection("questions")
    print("已删除旧向量集合")
except Exception:
    pass
vectors._collection.cache_clear()   # 清掉缓存的旧集合句柄

# 2) 重算所有题的向量
db = SessionLocal()
qs = db.query(Question).filter(Question.topic_summary != "",
                               Question.status != 2).all()
print("待重算题数:", len(qs))
ok = 0
for i, q in enumerate(qs, 1):
    try:
        vid = vectors.upsert(q.id, q.topic_summary, q.scope, q.category_l2)
        q.vector_id = vid
        ok += 1
        if i % 20 == 0:
            db.commit()
            print(f"  {i}/{len(qs)} …")
    except Exception as e:
        print("失败 q=%d: %s" % (q.id, e))
        break
db.commit()
db.close()
print(f"完成,成功重算 {ok} 道题的向量。")
