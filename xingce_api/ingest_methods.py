# -*- coding: utf-8 -*-
"""把方法论知识库向量化导入 Chroma(methods collection)。
用法:配好 .env 的 XC_EMBED_KEY 后,在 xingce_api 目录运行  python ingest_methods.py
(method_vectors.search 也会在库空时自动重建,本脚本用于手动/部署时预热与验证。)"""
import sys

import method_vectors

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


if __name__ == "__main__":
    n = method_vectors.rebuild()
    print(f"方法论已向量化入库: {n} 条", flush=True)
    # 自测一次跨模块硬过滤:资料分析的查询不应串到判断推理
    hits = method_vectors.search("某地区GDP同比增长率是多少", "资料分析", "资料分析", k=3)
    print("自测[资料分析]命中:", [(h["id"], h["module"]) for h in hits], flush=True)
    hits = method_vectors.search("下列图形的变化规律", "判断推理", "图形推理", k=3)
    print("自测[图形推理]命中:", [(h["id"], h["module"]) for h in hits], flush=True)
