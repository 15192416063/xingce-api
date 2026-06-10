# -*- coding: utf-8 -*-
"""
第三步：输入一道题(或一个主题)，从题库找出考点最相似的题。
需要 OpenAI API key。
用法：python step3_search.py
前置：先跑完 step2_build.py 建好 xingce_db
"""
from langchain_openai import OpenAIEmbeddings, ChatOpenAI
from langchain_community.vectorstores import Chroma

embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
vectorstore = Chroma(persist_directory="./xingce_db", embedding_function=embeddings)
llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)

# 关键：用户输入也要走同样的"提炼考点"流程，保证和库里用同一把尺子衡量
def extract_topic(content):
    prompt = f"""你是行测命题专家。请用一句话提炼下面内容的核心考点，
格式：题型 + 考点 + 主题。只输出这一句话。

内容：
{content}

考点："""
    return llm.invoke(prompt).content.strip()

def find_similar(user_input, k=3, threshold=1.0):
    topic = extract_topic(user_input)
    print(f"\n[识别到的考点] {topic}\n")
    # 带相似度分数检索；分数越小越相似(L2距离)
    results = vectorstore.similarity_search_with_score(topic, k=k)
    found = False
    for doc, score in results:
        if score > threshold:   # 超过阈值=不够相似，跳过
            continue
        found = True
        print(f"【相似度 {score:.3f}】题{doc.metadata['id']} — {doc.metadata['topic']}")
        print(doc.metadata["content"][:120], "...\n")
    if not found:
        print("题库中没有找到足够相似的题。")

if __name__ == "__main__":
    # 示例：用户贴进来一道题，找库里相似的
    sample = """某道题：阅读下面文字，作者意在强调消费对经济发展的重要作用。
    A.消费水平制约经济 B.消费规模巨大 C.消费已完成升级 D.消费对经济有持久拉动力"""
    find_similar(sample, k=3)
