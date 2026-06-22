# -*- coding: utf-8 -*-
"""换 embedding 模型 / 导入种子后,重建向量库。
注意:旧版用逐条 vectors.upsert 会让 chromadb(1.5.x)的 HNSW 索引不落盘,
重启后跨进程检索失效("系统没有数据/AI找不到题")。现统一改走批量 add,
直接委托给 rebuild_vectors_batch(整目录重建 + 单次批量 add + 回写 vector_id)。
用法:配好 .env 的 XC_EMBED_KEY 后,运行  python reembed.py
"""
import rebuild_vectors_batch

if __name__ == "__main__":
    rebuild_vectors_batch.main()
