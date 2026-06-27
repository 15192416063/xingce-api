# -*- coding: utf-8 -*-
"""向量库封装(Chroma)。存考点摘要向量,带 scope/l2 元数据供过滤检索。"""
import functools
import chromadb
import config
import ai


@functools.lru_cache(maxsize=1)
def _client():
    """同进程单例 Chroma client(chromadb 不允许同路径多实例)。
    方法论向量库 method_vectors 复用本 client,只是另开一个 collection。"""
    return chromadb.PersistentClient(path=config.VECTOR_DIR)


@functools.lru_cache(maxsize=1)
def _collection():
    return _client().get_or_create_collection("questions")


def upsert(question_id: int, summary: str, scope: str, l2: str):
    """写入/更新一条向量,返回 vector_id"""
    vid = str(question_id)
    _collection().upsert(
        ids=[vid],
        embeddings=[ai.embed(summary)],
        metadatas=[{"scope": scope, "l2": l2 or "", "qid": question_id}],
        documents=[summary or ""],
    )
    return vid


def delete(question_id: int):
    try:
        _collection().delete(ids=[str(question_id)])
    except Exception:
        pass


def search(summary: str, scopes, k: int = 5, exclude_qid: int = None, l2: str = None):
    """按考点摘要找相似;scope 过滤 + 可选题型(l2)过滤(避免图形推理串到资料分析)。
    返回 [(qid, distance)]"""
    try:
        qvec = ai.embed(summary)
        cond = [{"scope": {"$in": list(scopes)}}]
        if l2:
            cond.append({"l2": l2})
        where = cond[0] if len(cond) == 1 else {"$and": cond}
        res = _collection().query(query_embeddings=[qvec],
                                  n_results=k + 3, where=where)
    except Exception:
        return []   # 未配key/维度不匹配(换模型后需 reembed)/调用失败 → 不崩
    out = []
    ids = res.get("ids", [[]])[0]
    dists = res.get("distances", [[]])[0]
    for i, vid in enumerate(ids):
        qid = int(vid)
        if exclude_qid and qid == exclude_qid:
            continue
        out.append((qid, dists[i]))
        if len(out) >= k:
            break
    return out
