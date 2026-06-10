# 行测相似题检索系统 — DeepSeek 完整版 运行说明

## 这是什么
一份完整可运行的程序（app_deepseek.py），配好你的 DeepSeek key 就能在本地跑起来：
- 管理员上传 PDF → 自动切题、提炼考点 → 存入公共题库
- 任何人输入一道题 → 找出考点最相似的题

## 技术组合说明（重要）
- **提炼考点用 DeepSeek**（deepseek-chat）
- **算向量用本地开源中文模型 bge** —— 因为 DeepSeek 不提供 embedding（向量）接口，
  而本系统核心要算向量。bge 免费、中文好、数据不出本地，首次运行自动下载（约400MB）。

---

## 运行步骤（照着做）

### 第一步：装 Python（如果没有）
确认电脑有 Python 3.10+：终端运行 `python --version`。
没有就去 python.org 装，安装时勾选 "Add Python to PATH"。

### 第二步：装依赖
终端运行（一行）：
```
pip install streamlit pdfplumber langchain langchain-community langchain-openai langchain-huggingface sentence-transformers
```
这一步会装挺多东西（含本地向量模型的运行库），耐心等几分钟。

### 第三步：配置 DeepSeek key（二选一）

**方式A：环境变量（推荐）**
```
Windows:   set DEEPSEEK_API_KEY=你的key
Mac/Linux: export DEEPSEEK_API_KEY=你的key
```
注意：这种方式关掉终端就失效，每次开新终端要重设。

**方式B：直接填进代码**
打开 app_deepseek.py，找到这一行：
```
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "在这里填入你的DeepSeek_key")
```
把 "在这里填入你的DeepSeek_key" 换成你的真实 key。
（注意：填进代码后别把文件分享出去，会泄露key）

### 第四步：启动
在 app_deepseek.py 所在文件夹，终端运行：
```
streamlit run app_deepseek.py
```
浏览器会自动打开页面。

> ⚠️ 首次启动会下载本地向量模型（约400MB），需要联网、等一两分钟，下载一次以后就快了。

---

## 怎么用
1. **先上传题目**：点「🔑 管理员上传」标签 → 输入口令 admin123 → 上传一份行测PDF
   （可以先用包里的样本试卷，或你自己的）
   → 等它切题、提炼考点、入库完成。

2. **再检索**：点「🔍 找相似题」标签 → 粘贴一道题或描述一个主题 → 点检索
   → 看返回的相似题。

3. 调「相似度阈值」：返回结果不满意时，调大=更宽松多返回，调小=更严格。

---

## 常见问题
- **报错"还没配置key"** → 第三步没做对，检查key有没有设对。
- **下载向量模型卡住** → 检查网络；模型来自 HuggingFace，国内访问可能慢，多试几次或挂梯子。
- **切题数量不对** → 你的PDF题号格式可能和样本不同，看 05_常见问题速查 里的切题排查。
- **检索结果不准** → 先确认题库里已经入了足够的题；阈值调大些试试。

---

## 上线前必改（这是验证版，不是成品）
1. ADMIN_PASSWORD 改掉，换成正规登录
2. 本地 Chroma 换成 Qdrant/Milvus（支持多用户）
3. 加用户系统、私有题库（scope=user:xxx）、会员功能
详见包里的「02_技术方案书」和「01_开发提示词」。

## 关于题库内容
公共题库放的题必须来源干净（自己原创/AI生成/正版采购），
不要放网上抓的机构整理真题。上线前请咨询律师。
