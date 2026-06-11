# 行测智能题库 · 上岸助手(完全免费版)

完整可上线系统:账号体系 + 管理端/用户端 + AI 切题抠图 + 刷题/错题/收藏/数据 + 运营统计 + 考试资讯。**无会员/无付费墙**,成本控制内建。

## 核心闭环:用户 PDF → 私有题库 → 对话调题
1. 用户在首页「我的题库」点 **传PDF** → `/api/mine/upload-pdf` → AI 切题/分类/抠图,全部进 `scope=user:{id}` 私有库(自动入池,无需审核)。
2. 在 AI 对话框说「来几道图形推理」→ `/api/ai/ask` 先按题型从**用户自己上传的题**里精确调取,不够再向量检索补(私库+公共库),推荐卡片带「📤 我的上传」标识。
3. 限额(防滥用):私库 `XC_MINE_CAP`(默认2000题)、PDF `XC_MINE_PDF_MAX_MB`(30MB)、每日 `XC_MINE_PDF_DAILY`(3份)、AI 每日 `XC_AI_DAILY_CAP`(30次)。

## 省钱机制(免费系统的命脉)
- **词表秒分类**:「来几道图形推理」这类意图本地词表直接命中,**不调 LLM**;只有贴完整题(>60字)才调 classify。
- **闲聊不分析**:没有题型意图的短消息只走一次 chat,不做分类/推荐。
- **对话历史瘦身**:只带最近4条、每条600字。
- **embedding 查询缓存**:相同查询不重复调 API。
- **token 全量记账**:每次 LLM 调用(含 PDF 切题)的 token 进 `stat_daily` 表,管理员面板实时看成本(单价 `XC_PRICE_IN/OUT` 可调)。

## 运营统计
- **在线人数**(5分钟内活跃,内存心跳):所有登录用户可见(首页 pill + 侧栏);
- **PV/UV/对话次数/token消耗/成本估算/注册数/题库规模/近14天趋势**:仅管理员,「运营管理」页 `/api/admin/metrics`。

## 留存设计
段位体系(萌新→童生→秀才→…→状元,按累计做题数,零成本)、每日目标进度条(`XC_DAILY_GOAL` 默认20题)、连续打卡、在线人数社交激励。

前端:未登录展示**落地页**(顶栏右上角登录/注册、英雄区动画、功能介绍、滚动浮现),登录后进应用。

## 技术栈
Python · FastAPI · SQLAlchemy(SQLite,可切 MySQL) · Chroma 向量 · PyMuPDF 抠图 ·
DeepSeek(切题/分类) · 本地 bge(向量)。前端为单文件 SPA(零构建)。

## 模块
| 文件 | 职责 |
|---|---|
| `auth.py` | 密码哈希(PBKDF2+盐)、签名令牌(HMAC)、登录/管理员依赖 |
| `db.py` | 用户/题目/材料组/做题/错题/收藏/资讯等表 |
| `ai.py` | DeepSeek 切题分类 + bge 向量 |
| `extract_graphics.py` / `extract_material.py`(上级) | 题号锚定精确抠图 + 资料分析材料组 |
| `textutil.py` | 自适应去噪(页眉/页脚/水印,跨卷通用) |
| `ingest.py` | 入库编排(抠图+切题+去重+落库) |
| `main.py` | 全部 API(认证/出题/判分/统计/资讯/管理) |
| `stats.py` | 运营统计(在线心跳/PV/UV/对话/token 记账) |
| `static/index.html` | 前端 SPA(落地页 + 6 大模块) |

## 本地运行
```bash
pip install -r requirements.txt
cp .env.example .env        # 填入 DEEPSEEK_API_KEY 和 XC_SECRET_KEY
uvicorn main:app            # http://127.0.0.1:8000
```
**第一个注册的账号自动成为管理员**(也可注册时填 `XC_ADMIN_CODE`)。管理员可上传 PDF、审核、发资讯;普通用户刷题。

## 安全要点(上线必看)
1. **`XC_SECRET_KEY` 必须用环境变量设成随机长串**——否则令牌可被伪造(默认是每次重启随机,登录会失效)。`/api/health` 的 `secret_default=true` 即提醒未设置。
2. 密码只存 PBKDF2 哈希 + 每用户随机盐,绝不存明文;比对用 `compare_digest` 防时序攻击。
3. `.env`、`*.db`、`data/`、`vector_db/` 已在 `.gitignore`——**含 API key 和真题,切勿提交/公开**。
4. 数据按登录用户隔离(做题/错题/收藏/私有题库 scope 后端强校验)。

## 部署
- **Docker**(推荐):
  ```bash
  docker build -f xingce_api/Dockerfile -t xingce .   # 在仓库根执行
  docker run -p 8000:8000 --env-file xingce_api/.env -v $PWD/xcdata:/app/xingce_api/data xingce
  ```
- **云主机**:`uvicorn main:app --host 0.0.0.0 --port 8000 --workers 2`,前置 Nginx + HTTPS。
- **数据库**:上线建议 MySQL,设 `XC_DB_URL=mysql+pymysql://...`(需 `pip install pymysql`)。
- **对象存储**:`data/images` 量大后建议改存阿里云 OSS。

## 已知限制 / 下一步
- **题目答案未提取**:`answer` 为空,故正确率显示「—」、判分提示「未录入」。下一步做"答案解析页提取"即可让判分/正确率全量可用。
- 资料分析跨页材料(选项图在下一页)等极端排版靠「待确认」人工关兜底。
- 多 worker 下后台入库任务建议后续上 Redis 队列(当前用进程内 BackgroundTasks)。
