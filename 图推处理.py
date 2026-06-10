# -*- coding: utf-8 -*-
"""
============================================================
图形推理题处理工具（用通义千问 qwen-vl 多模态）
============================================================
配套主程序：app主程序_DeepSeek完整版_已修复.py
（用同一个题库 xingce_db、同一个向量模型，图推题入库后能和其他题一起检索）

为什么单独一个工具：
  图推题题干全是图，DeepSeek 看不了图，必须用"能看图"的多模态模型。
  这里用通义千问 qwen-vl（阿里，国内可用）。
  且图推识别准确率有限，需要你人工核对，所以做成"先识别→你审核→再入库"三步。

============================================================
【准备：申请通义 key】
============================================================
1. 打开 https://dashscope.console.aliyun.com/
2. 用阿里云账号登录（没有就注册，免费）
3. 开通"灵积模型服务 DashScope"，在 API-KEY 管理里创建一个 key
4. qwen-vl 有免费额度，验证阶段够用

============================================================
【运行步骤】
============================================================
1. 装依赖：
   pip install dashscope PyMuPDF langchain langchain-community langchain-huggingface

2. 设通义 key（二选一）：
   方式A：环境变量
     Windows:   set DASHSCOPE_API_KEY=你的key
     Mac/Linux: export DASHSCOPE_API_KEY=你的key
   方式B：直接填下面代码里的 DASHSCOPE_API_KEY

3. 第一步——识别图推题，生成待审核文件：
   python 图推处理.py --pdf 你的卷子.pdf --pages 4,5
   （--pages 填图推题在第几页，看你的卷子，逗号分隔多页）

4. 打开生成的 graphic_review.json，逐题核对"规律描述"对不对，
   不对就直接改这个文件（它就是普通文本，记事本能改）。

5. 第二步——把审核后的结果入库：
   python 图推处理.py --commit graphic_review.json
   入库后，图推题就能在主程序里和其他题一起被检索到。
============================================================
"""
import os
import sys
import json
import base64
import argparse

# ===== 配置（与主程序保持一致，勿随意改 DB_DIR 和 EMBEDDING_MODEL）=====
DB_DIR = "./xingce_db"
EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"
DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "在这里填入你的通义key")
VL_MODEL = "qwen-vl-max"  # 通义多模态模型；也可用 qwen-vl-plus（更便宜）


def render_page(pdf_path, page_num, out):
    """把PDF某页渲染成高清图（page_num从1开始）"""
    import fitz
    doc = fitz.open(pdf_path)
    pix = doc[page_num - 1].get_pixmap(matrix=fitz.Matrix(2, 2))
    pix.save(out)
    return out


def img_to_base64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def recognize_graphics(img_path):
    """用通义多模态识别一页里的所有图推题规律，返回列表"""
    import dashscope
    dashscope.api_key = DASHSCOPE_API_KEY

    prompt = """你是行测图形推理专家。这张图里有一道或多道图形推理题（题号形如"32."）。
请对每道图形推理题，识别它考查的图形规律，按下面JSON数组格式输出（只输出JSON，不要任何解释文字）：
[{"id": "题号", "rule_type": "规律大类", "rule_detail": "具体规律的文字描述"}]

规律大类只能从这些里选其一：样式规律 / 位置规律 / 数量规律 / 属性规律 / 空间重构

如果某道题你看不准具体规律，rule_detail 就写"规律不明确，需人工确认"，
但 rule_type 仍尽量归入一个最可能的大类。
只输出图形推理题，文字题不要管。"""

    b64 = img_to_base64(img_path)
    resp = dashscope.MultiModalConversation.call(
        model=VL_MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"image": f"data:image/png;base64,{b64}"},
                {"text": prompt},
            ]
        }]
    )
    try:
        content = resp.output.choices[0].message.content
        if isinstance(content, list):
            text = "".join(c.get("text", "") for c in content)
        else:
            text = str(content)
        text = text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception as e:
        print(f"⚠️ 解析模型返回失败：{e}")
        print("原始返回：", resp)
        return []


def cmd_recognize(pdf, pages):
    all_results = []
    for pg in pages.split(","):
        pg = pg.strip()
        img = render_page(pdf, int(pg), f"_page{pg}.png")
        print(f"正在识别第 {pg} 页…")
        res = recognize_graphics(img)
        for r in res:
            r["page"] = pg
        all_results += res
        print(f"  第 {pg} 页识别到 {len(res)} 道图推题")

    with open("graphic_review.json", "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print(f"\n共识别 {len(all_results)} 道图推题，已存入 graphic_review.json")
    print("=" * 50)
    print("下一步：打开 graphic_review.json 核对每道题的规律描述，")
    print("   改好后运行：python 图推处理.py --commit graphic_review.json")
    print("=" * 50)


def cmd_commit(json_file):
    from langchain_huggingface import HuggingFaceEmbeddings
    from langchain_community.vectorstores import Chroma
    from langchain_core.documents import Document

    with open(json_file, encoding="utf-8") as f:
        items = json.load(f)
    if not items:
        print("文件里没有题目，退出。")
        return

    print("正在加载向量模型并入库…")
    emb = HuggingFaceEmbeddings(model_name=EMBEDDING_MODEL)
    vs = Chroma(persist_directory=DB_DIR, embedding_function=emb)

    docs = []
    for r in items:
        rule_type = r.get("rule_type", "未知")
        rule_detail = r.get("rule_detail", "")
        topic = f"图形推理-{rule_type}-{rule_detail}"
        content = (f"【图形推理题】第{r.get('id')}题\n"
                   f"考查规律：{rule_type} — {rule_detail}\n"
                   f"（图形题，原图见试卷第{r.get('page')}页）")
        docs.append(Document(
            page_content=topic,
            metadata={"content": content, "topic": topic,
                      "scope": "public", "category": "图形推理"}
        ))
    vs.add_documents(docs)
    print(f"完成！{len(docs)} 道图推题已存入题库，现在能在主程序里和其他题一起检索了。")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="图推题多模态处理工具")
    ap.add_argument("--pdf", help="试卷PDF路径")
    ap.add_argument("--pages", help="图推题所在页码，逗号分隔，如 4,5")
    ap.add_argument("--commit", help="把审核后的json入库")
    args = ap.parse_args()

    if args.commit:
        cmd_commit(args.commit)
    elif args.pdf and args.pages:
        if DASHSCOPE_API_KEY == "在这里填入你的通义key":
            print("还没配置通义 key！请看脚本顶部说明：")
            print("   方式A：设环境变量 DASHSCOPE_API_KEY")
            print("   方式B：填入脚本顶部的 DASHSCOPE_API_KEY")
            sys.exit(1)
        cmd_recognize(args.pdf, args.pages)
    else:
        print("用法：")
        print("  识别图推题：python 图推处理.py --pdf 卷子.pdf --pages 4,5")
        print("  审核后入库：python 图推处理.py --commit graphic_review.json")