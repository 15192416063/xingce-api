# -*- coding: utf-8 -*-
"""
============================================================
行测相似题检索系统 — 完整可运行版（DeepSeek + 本地中文向量）
============================================================

功能：
  - 管理员上传行测PDF → 自动切题、提炼考点 → 存入公共题库
  - 任何人输入一道题/主题 → 找出考点最相似的题
  - 题目带 scope 标记，预留"用户私有题库"扩展

技术组合（针对 DeepSeek 用户）：
  - 提炼考点：DeepSeek (deepseek-chat)
  - 算向量：本地开源中文模型 bge（因为 DeepSeek 不提供 embedding 接口）
    bge 免费、中文效果好、数据不出本地。首次运行会自动下载模型(约400MB)。

============================================================
【运行步骤】
============================================================
1. 安装依赖（在终端运行）：
   pip install streamlit pdfplumber langchain langchain-community langchain-openai langchain-huggingface sentence-transformers

2. 设置你的 DeepSeek API key（二选一）：
   方式A - 临时设置（关terminal就失效）：
     Windows:  set DEEPSEEK_API_KEY=你的key
     Mac/Linux: export DEEPSEEK_API_KEY=你的key
   方式B - 直接填在下面代码里（找到 DEEPSEEK_API_KEY = ）

3. 启动：
   streamlit run app_deepseek.py
   浏览器会自动打开，开始使用

============================================================
"""
import os
import re
import json
import tempfile
import streamlit as st
import pdfplumber
from langchain_openai import ChatOpenAI
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_community.vectorstores import Chroma
from langchain_core.documents import Document

# ============================================================
# 配置区（按需修改）
# ============================================================
# DeepSeek API key：优先读环境变量，没有就用下面这行（把你的key填进引号）
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "在这里填入你的DeepSeek_key")

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
LLM_MODEL = "deepseek-chat"

# 本地中文向量模型（首次运行自动下载）。bge-small 更小更快，bge-large 效果更好
EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"

DB_DIR = "./xingce_db"            # 向量库存储位置
ADMIN_PASSWORD = "admin123"       # 管理员口令，上线务必改掉

# 通义千问多模态 key（用于精准裁剪资料分析图表）。没有就留空，会自动退回整页截图。
# 申请地址：bailian.console.aliyun.com
DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "在这里填入你的通义key")  # 填入你的通义key，留空则用整页截图
VL_MODEL = "qwen3-vl-plus"        # 通义多模态模型

# 图形题特征话术，用于过滤（不能靠字数，否则误删类比题）
GRAPHIC_MARKERS = ["填入问号处", "呈现一定的规律", "从所给的四个选项"]


# ============================================================
# 多模态精准裁剪图表（用通义 qwen-vl）
# ============================================================
def detect_chart_bbox(page_image_path):
    """
    让通义 qwen-vl 看整页图，返回图表的归一化坐标 [x0,y0,x1,y1]（0~1）。
    识别不到或未配置key则返回 None（调用方会退回整页截图）。
    """
    if not DASHSCOPE_API_KEY:
        print("[图表裁剪] 未配置通义key（DASHSCOPE_API_KEY为空），用整页截图")
        return None
    try:
        import dashscope, base64
        dashscope.api_key = DASHSCOPE_API_KEY
        with open(page_image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        prompt = """这是一页行测资料分析题的截图。请判断页面里有没有需要保留的"视觉材料"——
包括：统计图表（柱状图、折线图、饼图、散点图等）、数据表格、或任何包含数据的图形。
只要有上述任意一种，就框出它的位置。

返回这个视觉材料区域的归一化边界框坐标（页面左上角0,0，右下角1,1），
格式严格为JSON：{"has_chart": true/false, "bbox": [x0, y0, x1, y1]}
- 如果有图表或数据表格，has_chart填true，bbox框住它（可以框大一点，把完整图表/表格都包进去）。
- 只有当整页完全是纯文字、没有任何图表和表格时，才填false。
只输出JSON，不要解释。"""
        resp = dashscope.MultiModalConversation.call(
            model=VL_MODEL,
            messages=[{"role": "user", "content": [
                {"image": f"data:image/png;base64,{b64}"},
                {"text": prompt}]}])
        content = resp.output.choices[0].message.content
        text = "".join(c.get("text", "") for c in content) if isinstance(content, list) else str(content)
        text = text.replace("```json", "").replace("```", "").strip()
        import json as _json
        data = _json.loads(text)
        if data.get("has_chart") and data.get("bbox"):
            bbox = data["bbox"]
            if len(bbox) == 4 and all(0 <= v <= 1 for v in bbox):
                print(f"[图表裁剪] 成功定位图表，坐标 {bbox}")
                return bbox
        print("[图表裁剪] 模型未识别到图表，用整页截图")
        return None
    except Exception as e:
        print(f"[图表裁剪] 调用通义出错（退回整页截图）：{e}")
        return None  # 任何出错都退回整页截图


# ============================================================
# 模型与向量库初始化（缓存，避免重复加载）
# ============================================================
@st.cache_resource
def get_llm():
    return ChatOpenAI(
        model=LLM_MODEL,
        temperature=0,
        api_key=DEEPSEEK_API_KEY,
        base_url=DEEPSEEK_BASE_URL,
    )

@st.cache_resource
def get_embeddings():
    # 本地中文向量模型
    return HuggingFaceEmbeddings(model_name=EMBEDDING_MODEL)

@st.cache_resource
def get_vectorstore(_emb):
    return Chroma(persist_directory=DB_DIR, embedding_function=_emb)


# ============================================================
# 切题（已在真实试卷验证）
# ============================================================
def format_question(text):
    """把挤成一团的题目格式化：题干一段 + ABCD选项各自换行，便于阅读"""
    text = text.strip()
    # 在每个选项标记 A. B. C. D.（兼容 A、）前插入换行
    text = re.sub(r'\s*([ABCD][.、])', r'\n\1', text)
    lines = []
    for line in text.split('\n'):
        line = re.sub(r'[ \t]+', '', line.strip())  # 去掉中文里的多余空格
        if line:
            lines.append(line)
    return '\n\n'.join(lines)  # 段落间空一行，更清爽


# ============================================================
# 智能切题：用 DeepSeek 理解各种排版，不依赖固定题号格式
# ============================================================
def _split_batches(text, batch_size=3000, overlap=200):
    """长文本切成有重叠的批次，避免题目被从中间切断"""
    batches, start = [], 0
    while start < len(text):
        batches.append(text[start:start + batch_size])
        start += batch_size - overlap
    return batches


def _parse_json(resp_text):
    """解析LLM返回的JSON，带容错"""
    resp_text = resp_text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(resp_text)
    except json.JSONDecodeError:
        m = re.search(r'\[.*\]', resp_text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return []
        return []


def smart_split_questions(text, llm, progress_callback=None):
    """
    用 DeepSeek 智能切题，自适应各种排版。
    返回 (题目列表, 诊断信息)。题目每项 {content, type}。
    会跳过图形推理题（题干是图、无法处理）。
    """
    all_qs = []
    diag = {"batches": 0, "ok_batches": 0, "errors": []}
    batches = _split_batches(text)
    diag["batches"] = len(batches)

    for i, batch in enumerate(batches):
        prompt = f"""你是行测试卷解析专家。下面是一份行测试卷的一段文字（可能从中间截断）。
请把其中**完整的**单道题目提取出来，每道题包含题干和它的所有选项。

注意这份试卷的排版特点（要能适应各种排版）：
- 题号可能在行首（如"1 "、"1."、"1、"），也可能漂在题干中间或括号附近，要靠理解判断。
- 选项标记可能是"A."也可能是"A、"。
- 既有单项选择题，也有多项选择题，都要提取。

要求：
1. 只提取完整的题（题干+选项齐全）。被从中间截断的不完整的题，忽略。
2. 判断题型，type 只能填：言语理解/常识判断/类比推理/逻辑判断/数量关系/资料分析/图形推理/其他
3. 图形推理题（主要靠图形、题干像"选出符合规律的"）也标出来，type填"图形推理"
4. 申论、材料写作题不要提取
5. 只输出JSON数组，不要任何解释文字：
[{{"content": "完整题目原文(题干+所有选项)", "type": "题型"}}]

试卷文字：
{batch}

JSON："""
        try:
            resp = llm.invoke(prompt).content
            qs = _parse_json(resp)
            if qs:
                all_qs.extend(qs)
                diag["ok_batches"] += 1
            else:
                diag["errors"].append(f"第{i+1}批：模型返回无法解析成题目")
        except Exception as e:
            diag["errors"].append(f"第{i+1}批调用出错：{e}")
        if progress_callback:
            progress_callback((i + 1) / len(batches))

    # 去重
    seen, result = set(), []
    for q in all_qs:
        content = q.get("content", "")
        key = re.sub(r'\s', '', content[:30])
        if key and key not in seen:
            seen.add(key)
            result.append(q)
    return result, diag


def parse_pdf(file_path):
    """
    返回两类题：
      normal_questions: 普通文字题列表
      data_questions: 资料分析题，每项是 (材料文本, 小题文本, 图片路径列表)
        图片是该组资料分析所在页的渲染图，含图表，供完整展示。
    图形题被跳过。
    """
    import fitz
    # 用 PyMuPDF 逐页提取文字，同时记录每页文字，便于定位资料分析在哪几页
    doc = fitz.open(file_path)
    page_texts = [p.get_text() for p in doc]
    full = "\n".join(page_texts)

    # 记录每页在 full 中的字符起始位置，用于把文本位置映射回页码
    page_offsets = []
    pos = 0
    for t in page_texts:
        page_offsets.append(pos)
        pos += len(t) + 1  # +1 是join的换行

    def pos_to_page(char_pos):
        """字符位置 → 页码(0-based)"""
        pg = 0
        for i, off in enumerate(page_offsets):
            if char_pos >= off:
                pg = i
            else:
                break
        return pg

    # 1. 砍掉申论
    for marker in ["五、材料处理题", "材料处理题"]:
        idx = full.find(marker)
        if idx != -1:
            full = full[:idx]
            break

    # 准备存图目录
    img_dir = "question_images"
    os.makedirs(img_dir, exist_ok=True)
    rendered = {}  # 页码 -> 图片路径，避免重复渲染

    def render(pg):
        if pg in rendered:
            return rendered[pg]
        page = doc[pg]
        # 先整页渲染（作为底图和降级方案）
        full_out = os.path.join(img_dir, f"page_{pg+1}.png")
        page.get_pixmap(matrix=fitz.Matrix(2, 2)).save(full_out)
        # 尝试用多模态精准定位图表并裁剪
        bbox = detect_chart_bbox(full_out)
        if bbox:
            r = page.rect
            x0, y0, x1, y1 = bbox
            # 加安全边距（四周各放宽5%），保证图表不被裁掉
            mx, my = 0.05 * (x1 - x0), 0.05 * (y1 - y0)
            x0, y0 = max(0, x0 - mx), max(0, y0 - my)
            x1, y1 = min(1, x1 + mx), min(1, y1 + my)
            clip = fitz.Rect(x0 * r.width, y0 * r.height, x1 * r.width, y1 * r.height)
            crop_out = os.path.join(img_dir, f"chart_{pg+1}.png")
            page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=clip).save(crop_out)
            rendered[pg] = crop_out      # 用裁剪后的精准图
            return crop_out
        rendered[pg] = full_out          # 没识别到图表，用整页
        return full_out

    # 2. 资料分析材料组
    # 材料组引导语，兼容多种写法：
    #   "（一）根据下列材料"、"一、根据以下资料"、"根据下列资料回答1~5题" 等
    group_pat = re.compile(
        r'(?:（?[一二三四五六七八九十]+[）、.]?\s*)?根据(?:以下|下列)(?:资料|材料)')
    groups = list(group_pat.finditer(full))

    data_questions = []
    if groups:
        full_normal = full[:groups[0].start()]
        for i, g in enumerate(groups):
            s = g.start()
            e = groups[i + 1].start() if i + 1 < len(groups) else len(full)
            block = full[s:e]
            subs = list(re.compile(r'(?m)^(\d{1,3})\.').finditer(block))
            if not subs:
                continue
            material = block[:subs[0].start()].strip()
            # 这组资料分析覆盖的页码范围 → 渲染成图片
            start_pg = pos_to_page(s)
            end_pg = pos_to_page(e - 1)
            imgs = [render(pg) for pg in range(start_pg, end_pg + 1)]
            for j, sm in enumerate(subs):
                ss = sm.start()
                se = subs[j + 1].start() if j + 1 < len(subs) else len(block)
                sub_q = block[ss:se].strip()
                data_questions.append((material, sub_q, imgs))
    else:
        full_normal = full

    # 3. 普通题区域的文字，交给智能切题处理（不再用死正则）
    return full_normal, data_questions


# ============================================================
# 提炼考点（DeepSeek）
# ============================================================
def extract_topic(content, llm):
    prompt = f"""你是行测命题专家。请用一句话提炼下面这道行测题的核心考点，
格式：题型 + 考点 + 主题。例如"言语理解-片段阅读-意图判断-经济发展主题"。
只输出这一句话，不要解释。

题目：
{content}

考点："""
    return llm.invoke(prompt).content.strip()


def extract_topic_data(material, sub_q, llm):
    """资料分析题：带材料背景提炼考点（只看小题会残缺）"""
    prompt = f"""你是行测命题专家。下面是一道资料分析题，含共享材料和具体小题。
请用一句话提炼这道小题的核心考点，格式：资料分析 + 考查能力 + 主题。
例如"资料分析-增长率计算-经济数据主题"。只输出这一句话，不要解释。

【共享材料（节选）】
{material[:500]}

【小题】
{sub_q}

考点："""
    return llm.invoke(prompt).content.strip()


# ============================================================
# 界面
# ============================================================
st.set_page_config(page_title="行测相似题检索", page_icon="📝")
st.title("📝 行测相似题检索系统")
st.caption("DeepSeek 提炼考点 + 本地中文向量 · 公共题库人人可搜")

# key 检查
if DEEPSEEK_API_KEY == "在这里填入你的DeepSeek_key":
    st.error("⚠️ 还没配置 DeepSeek API key！请看代码顶部说明，用环境变量或直接填入。")
    st.stop()

llm = get_llm()
with st.spinner("首次启动会下载本地向量模型（约400MB），请稍候…"):
    emb = get_embeddings()
vs = get_vectorstore(emb)

tab_search, tab_admin = st.tabs(["🔍 找相似题（所有人）", "🔑 管理员上传"])

# ---------- 检索 ----------
with tab_search:
    st.subheader("输入一道题或一个主题，找出考点相似的题")
    user_q = st.text_area("题目 / 主题", height=140,
                          placeholder="粘贴一道行测题，或描述一个考点主题…")
    col1, col2 = st.columns(2)
    topk = col1.slider("返回数量", 1, 10, 5)
    threshold = col2.slider("相似度阈值（越小越严格）", 0.1, 2.0, 1.0, 0.1)
    if st.button("检索", type="primary"):
        if not user_q.strip():
            st.warning("请先输入内容")
        else:
            with st.spinner("识别考点并检索中…"):
                topic = extract_topic(user_q, llm)
                st.info(f"识别到的考点：{topic}")
                results = vs.similarity_search_with_score(
                    topic, k=topk, filter={"scope": "public"})
                hit = False
                for doc, score in results:
                    if score > threshold:
                        continue
                    hit = True
                    st.markdown(f"**相似度 {score:.3f}** · {doc.metadata.get('topic','')}")
                    # 格式化显示：题干一段 + ABCD各自换行，完整显示
                    formatted = format_question(doc.metadata.get("content", ""))
                    st.text(formatted)
                    # 资料分析题：显示关联的图表图片（含统计图）
                    imgs = doc.metadata.get("images", "")
                    if imgs:
                        for img_path in imgs.split("|"):
                            if img_path and os.path.exists(img_path):
                                st.image(img_path, caption="题目原页（含图表）", use_container_width=True)
                    # 显示试题来源
                    src = doc.metadata.get("source", "")
                    if src:
                        st.caption(f"📄 试题来源：{src}")
                    st.divider()
                if not hit:
                    st.warning("公共题库中没有找到足够相似的题。")

# ---------- 管理员上传 ----------
with tab_admin:
    st.subheader("管理员上传题目到公共题库")
    pwd = st.text_input("管理员口令", type="password")
    if pwd == ADMIN_PASSWORD:
        st.success("已验证管理员身份")
        up = st.file_uploader("上传行测 PDF（文字版）", type="pdf")
        if up and st.button("解析并入库"):
            # 用上传的文件名（去掉.pdf）作为这份卷子所有题的来源
            source_name = os.path.splitext(up.name)[0]
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                tmp.write(up.read())
                path = tmp.name
            with st.spinner("智能切题中（AI正在理解试卷排版，稍候）…"):
                normal_text, data_qs = parse_pdf(path)
                split_bar = st.progress(0)
                smart_qs, diag = smart_split_questions(
                    normal_text, llm, progress_callback=lambda p: split_bar.progress(p))
                normal_qs = [q for q in smart_qs if q.get("type") != "图形推理"]
                graphic_count = len(smart_qs) - len(normal_qs)
            total = len(normal_qs) + len(data_qs)

            # 显示切题诊断
            st.caption(f"切题诊断：共 {diag['batches']} 批文字，成功处理 {diag['ok_batches']} 批")
            if diag["errors"]:
                with st.expander("⚠️ 部分批次有问题（点击查看）"):
                    for err in diag["errors"]:
                        st.text(err)

            # 保护：如果一道题都没切出来，按诊断给出针对性提示
            if total == 0:
                os.unlink(path)
                st.error("⚠️ 这份 PDF 没有切出任何题目。")
                if diag["errors"]:
                    st.warning("看起来是 AI 调用出了问题（见上方诊断），常见原因："
                               "① DeepSeek 的 key 不对或余额不足；② 网络连不上 DeepSeek；"
                               "③ 返回格式异常。请检查 key 和网络后重试。")
                else:
                    st.info("文字提取正常但没识别出题目，可能这份PDF内容不是标准选择题。")
                st.stop()

            # 取出库里已有的所有题目指纹，用于去重
            def fingerprint(text):
                return re.sub(r'\s', '', text)[:50]  # 内容前50字（去空白）作指纹
            existing = set()
            try:
                all_meta = vs.get()  # 取出库里所有已存题目的metadata
                for md in all_meta.get("metadatas", []) or []:
                    fp = md.get("fp")
                    if fp:
                        existing.add(fp)
            except Exception:
                pass  # 空库或读取失败，按无重复处理

            st.write(f"智能切题完成：普通题 {len(normal_qs)} 道 + 资料分析题 {len(data_qs)} 道"
                     f"（图形题 {graphic_count} 道已跳过），来源：{source_name}，正在提炼考点入库…")
            docs, bar, done = [], st.progress(0), 0
            dup_count = 0  # 跳过的重复题数

            # 普通文字题
            for q in normal_qs:
                body = q.get("content", "")
                fp = fingerprint(body)
                done += 1
                bar.progress(done / total)
                if fp in existing:        # 已存在，跳过（去重）
                    dup_count += 1
                    continue
                existing.add(fp)
                topic = extract_topic(body, llm)
                docs.append(Document(
                    page_content=topic,
                    metadata={"content": body, "topic": topic, "scope": "public",
                              "category": "普通题", "source": source_name, "fp": fp}
                ))

            # 资料分析题（带材料；材料+小题一起作指纹）
            for material, sub_q, imgs in data_qs:
                full_content = f"【资料分析·共享材料】\n{material}\n\n【小题】\n{sub_q}"
                fp = fingerprint(material + sub_q)
                done += 1
                bar.progress(done / total)
                if fp in existing:
                    dup_count += 1
                    continue
                existing.add(fp)
                topic = extract_topic_data(material, sub_q, llm)
                docs.append(Document(
                    page_content=topic,
                    metadata={"content": full_content, "topic": topic, "scope": "public",
                              "category": "资料分析", "source": source_name,
                              "images": "|".join(imgs), "fp": fp}
                ))

            if docs:
                vs.add_documents(docs)
            os.unlink(path)
            msg = f"完成！新入库 {len(docs)} 道题（来源：{source_name}）。"
            if dup_count:
                msg += f" 另有 {dup_count} 道与库中已有题目重复，已自动跳过。"
            st.success(msg)
    elif pwd:
        st.error("口令错误")
    else:
        st.caption("输入管理员口令后可上传。默认 admin123，上线请务必修改。")