# -*- coding: utf-8 -*-
"""方法论向量库(Chroma collection: methods)——数据层。
把 method_kb.all_entries() 摊平的方法论条目向量化入库,检索时按 module 硬过滤
(资料分析的题绝不串到判断推理的方法论),再在过滤范围内做向量相似检索取 top-k。
与题库向量(vectors.py 的 questions)分属不同 collection、共用同一 Chroma client。"""
import functools

import ai
import vectors
import method_kb

_COLL = "methods"


@functools.lru_cache(maxsize=1)
def _collection():
    return vectors._client().get_or_create_collection(_COLL)


def count():
    try:
        return _collection().count()
    except Exception:
        return 0


def rebuild():
    """重建方法论向量库:整 collection 重建 + 批量 add(chromadb 1.5.x 逐条 upsert
    索引不落盘,批量 add 才能正确持久化)。需先配好 XC_EMBED_KEY。返回入库条数。"""
    entries = method_kb.all_entries()
    ids, embs, metas, docs = [], [], [], []
    for e in entries:
        ids.append(e["id"])
        embs.append(ai.embed(e["embed_text"]))
        metas.append({"module": e["module"], "question_type": e["question_type"],
                      "label": e.get("label", ""), "text": e["inject_text"]})
        docs.append(e["embed_text"])
    client = vectors._client()
    try:
        client.delete_collection(_COLL)
    except Exception:
        pass
    _collection.cache_clear()
    col = client.get_or_create_collection(_COLL)
    B = 100
    for i in range(0, len(ids), B):
        col.add(ids=ids[i:i + B], embeddings=embs[i:i + B],
                metadatas=metas[i:i + B], documents=docs[i:i + B])
    _collection.cache_clear()
    return len(ids)


def search(content, module, question_type=None, k=3):
    """硬过滤 module + 向量取 top-k 方法论。库空时自动重建一次(免手动预热)。
    返回 [{id, text, module, question_type, distance}];任何异常(没配 key/维度不符)→ []。"""
    try:
        if count() == 0:
            rebuild()
    except Exception:
        return []
    try:
        query = content or " ".join([module or "", question_type or ""]).strip()
        qvec = ai.embed(query)
        where = {"module": module} if module else None
        res = _collection().query(query_embeddings=[qvec], n_results=k, where=where)
    except Exception:
        return []
    out = []
    ids = (res.get("ids") or [[]])[0]
    metas = (res.get("metadatas") or [[]])[0]
    dists = (res.get("distances") or [[]])[0]
    for i, mid in enumerate(ids):
        m = metas[i] or {}
        out.append({"id": mid, "text": m.get("text", ""),
                    "label": m.get("label", ""),
                    "module": m.get("module", ""),
                    "question_type": m.get("question_type", ""),
                    "distance": dists[i] if i < len(dists) else None})
    return out
