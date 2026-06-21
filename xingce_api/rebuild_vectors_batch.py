# -*- coding: utf-8 -*-
"""稳健重建向量库:批量 embedding + 单次批量 add(chromadb 1.5.x 逐条 upsert 会
导致 HNSW 索引未落盘、重启后检索失效;批量 add 可正确持久化)。
从 xingce_demo.db 的 topic_summary 重新算向量(旧 chroma 已损坏无法读回)。
"""
import os
import sys
import time
import shutil

import requests
import chromadb

import config
from db import SessionLocal, Question

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def embed_batch(texts):
    """硅基流动 /embeddings 批量接口(OpenAI 兼容,input 接受数组),带重试。"""
    last = None
    for attempt in range(4):
        try:
            r = requests.post(
                config.EMBED_BASE_URL.rstrip("/") + "/embeddings",
                headers={"Authorization": "Bearer " + config.EMBED_KEY,
                         "Content-Type": "application/json"},
                json={"model": config.EMBED_MODEL, "input": texts},
                timeout=60)
            r.raise_for_status()
            data = r.json()["data"]
            # 按 index 排序,确保与输入一一对应
            data.sort(key=lambda d: d["index"])
            return [d["embedding"] for d in data]
        except Exception as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"embedding 批量失败: {last}")


def main():
    if not config.EMBED_KEY:
        raise SystemExit("缺少 XC_EMBED_KEY")
    db = SessionLocal()
    qs = db.query(Question).filter(Question.topic_summary != "",
                                   Question.status != 2).all()
    rows = [(q.id, q.topic_summary, q.scope, q.category_l2 or "") for q in qs]
    db.close()
    print(f"待向量化题数: {len(rows)}", flush=True)

    # 1) 批量 embedding
    ids, embs, metas, docs = [], [], [], []
    B = 16
    t0 = time.time()
    for i in range(0, len(rows), B):
        chunk = rows[i:i + B]
        vecs = embed_batch([r[1] for r in chunk])
        for (qid, summary, scope, l2), v in zip(chunk, vecs):
            ids.append(str(qid))
            embs.append(v)
            metas.append({"scope": scope, "l2": l2, "qid": qid})
            docs.append(summary or "")
        if (i // B) % 5 == 0:
            print(f"  embedding {min(i+B,len(rows))}/{len(rows)} "
                  f"({time.time()-t0:.0f}s)", flush=True)
    print(f"embedding 完成,共 {len(ids)} 条,耗时 {time.time()-t0:.0f}s", flush=True)

    # 2) 重建 collection:整目录清掉,批量 add(确保索引落盘)
    shutil.rmtree(config.VECTOR_DIR, ignore_errors=True)
    os.makedirs(config.VECTOR_DIR, exist_ok=True)
    client = chromadb.PersistentClient(path=config.VECTOR_DIR)
    col = client.get_or_create_collection("questions")
    AB = 200
    for i in range(0, len(ids), AB):
        col.add(ids=ids[i:i + AB], embeddings=embs[i:i + AB],
                metadatas=metas[i:i + AB], documents=docs[i:i + AB])
    print("批量 add 完成,in-proc count:", col.count(), flush=True)

    # 回写 vector_id(Chroma id 即 str(question_id)),保持 DB 记账一致
    db = SessionLocal()
    for qid_str in ids:
        row = db.get(Question, int(qid_str))
        if row:
            row.vector_id = qid_str
    db.commit()
    db.close()
    print("vector_id 已回写", len(ids), "条", flush=True)

    # 3) 同进程自测一次检索
    q = col.query(query_embeddings=[embs[0]], n_results=3)
    print("in-proc 自测 query ids:", q["ids"][0][:3], flush=True)


if __name__ == "__main__":
    main()
