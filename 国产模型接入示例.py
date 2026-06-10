# -*- coding: utf-8 -*-
"""
国产模型接入示例
================
你是中文场景、国内部署，大概率要把 OpenAI 换成 DeepSeek / 通义 / 智谱。
好消息：这些模型大多【兼容 OpenAI 接口】，所以代码几乎不用改，
只需改 base_url 和 model 名，仍然用 langchain-openai。

下面对比展示「原 OpenAI 写法」和「换成国产模型」的区别。
实际项目中，把这些值放进 .env 用环境变量读取，不要写死在代码里。
"""
import os
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

# ============================================================
# 一、LLM（提炼考点用）—— 三种写法对比
# ============================================================

# 【原 OpenAI 写法】
llm_openai = ChatOpenAI(
    model="gpt-4o-mini",
    temperature=0,
    api_key=os.getenv("OPENAI_API_KEY"),
)

# 【换 DeepSeek】只改 base_url 和 model
llm_deepseek = ChatOpenAI(
    model="deepseek-chat",
    temperature=0,
    api_key=os.getenv("OPENAI_API_KEY"),       # 填 DeepSeek 的 key
    base_url="https://api.deepseek.com/v1",     # ← 关键：改这里
)

# 【换通义千问】
llm_qwen = ChatOpenAI(
    model="qwen-plus",
    temperature=0,
    api_key=os.getenv("OPENAI_API_KEY"),       # 填 DashScope 的 key
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
)

# 【换智谱 GLM】
llm_glm = ChatOpenAI(
    model="glm-4-flash",
    temperature=0,
    api_key=os.getenv("OPENAI_API_KEY"),       # 填智谱的 key
    base_url="https://open.bigmodel.cn/api/paas/v4",
)

# ============================================================
# 二、Embedding（算向量用）—— 这里要特别注意！
# ============================================================
# ⚠️ 重点：不是所有国产 LLM 厂商都提供兼容 OpenAI 的 embedding 接口，
#    且各家 embedding 向量维度不同。务必确认两点：
#    1) 建库和检索用【同一个】embedding 模型，否则向量空间不一致，检索全乱。
#    2) 中文场景选中文支持好的模型。

# 【方案1：用通义的 embedding（兼容 OpenAI 接口）】
emb_qwen = OpenAIEmbeddings(
    model="text-embedding-v3",                  # 通义的 embedding 模型
    api_key=os.getenv("OPENAI_API_KEY"),        # DashScope key
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
)

# 【方案2：用本地开源中文 embedding（bge，免费、效果好、可私有化）】
# 需要：pip install sentence-transformers langchain-huggingface
# from langchain_huggingface import HuggingFaceEmbeddings
# emb_bge = HuggingFaceEmbeddings(
#     model_name="BAAI/bge-large-zh-v1.5",       # 中文 embedding 优秀选择
# )
# bge 的好处：免费、不依赖外部 API、数据不出本地（隐私友好）。
# 代价：要本地有一定算力，首次会下载模型。

# ============================================================
# 三、推荐做法：抽象成可配置，不要写死
# ============================================================
# 在正式代码里，建议这样封装，切换模型只改 .env，不动业务逻辑：
def get_llm():
    return ChatOpenAI(
        model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
        temperature=0,
        api_key=os.getenv("OPENAI_API_KEY"),
        base_url=os.getenv("OPENAI_BASE_URL"),   # 不设则默认 OpenAI 官方
    )

def get_embeddings():
    return OpenAIEmbeddings(
        model=os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"),
        api_key=os.getenv("OPENAI_API_KEY"),
        base_url=os.getenv("OPENAI_BASE_URL"),
    )

# ============================================================
# 四、快速自测（填好 .env 后运行：python 国产模型接入示例.py）
# ============================================================
if __name__ == "__main__":
    llm = get_llm()
    resp = llm.invoke("用一句话说明什么是行测的'言语理解'题型。")
    print("LLM 测试返回：", resp.content)

    emb = get_embeddings()
    vec = emb.embed_query("测试中文向量")
    print(f"Embedding 测试：返回向量维度 = {len(vec)}")
    print("✓ 如果上面两行都正常输出，说明模型接入成功。")
