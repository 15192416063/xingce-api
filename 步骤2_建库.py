# -*- coding: utf-8 -*-
"""
第二步：给每道题提炼"考点摘要"，算向量，存入向量库。
需要 OpenAI API key（环境变量 OPENAI_API_KEY）。
用法：python step2_build.py
前置：先跑完 step1_parse.py 生成 questions.json
"""
import json
from langchain_openai import OpenAIEmbeddings, ChatOpenAI
from langchain_community.vectorstores import Chroma
from langchain.schema import Document

# 读取第一步切好的题
with open("questions.json", encoding="utf-8") as f:
    questions = json.load(f)

# 用便宜的小模型给每道题提炼"考点摘要"
# 关键设计：用考点摘要(而非原题)去算向量，让"相似"精准落在考点维度
llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)

def extract_topic(content):
    prompt = f"""你是行测命题专家。请用一句话提炼下面这道行测题的核心考点，
格式：题型 + 考点 + 主题。例如"言语理解-片段阅读-意图判断-经济发展主题"。
只输出这一句话，不要解释。

题目：
{content}

考点："""
    return llm.invoke(prompt).content.strip()

docs = []
for q in questions:
    topic = extract_topic(q["content"])
    print(f"题{q['id']}: {topic}")
    # 用考点摘要做向量化的正文，原题完整存进 metadata
    docs.append(Document(
        page_content=topic,                    # ← 算向量用的是考点摘要
        metadata={"id": q["id"], "content": q["content"], "topic": topic}
    ))

# 中文用 text-embedding-3-small 够用；想要更好的中文效果可换成 bge 系列
embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
vectorstore = Chroma.from_documents(
    docs, embeddings, persist_directory="./xingce_db"
)
print(f"\n向量库已建立，共 {len(docs)} 道题")
