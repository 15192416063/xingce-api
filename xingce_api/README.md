# 行测智能题库 · 上岸助手

完整可上线系统:账号体系 + 管理端/用户端 + AI 切题抠图 + 刷题/错题/收藏/数据 + 会员变现 + 考试资讯。

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
| `main.py` | 全部 API(认证/出题/判分/会员/资讯/管理) |
| `static/index.html` | 前端 SPA(登录门 + 6 大模块) |

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

## 变现(内测 → 收费)
- 内测期 `XC_BETA_FREE=true`:会员功能全免费,先把用户跑起来。
- 正式收费改 `false`:非会员的「找相似」等返回 402 引导升级。
- **支付未接**:`/api/membership/upgrade` 是占位(内测直接开通)。正式收费需接**微信支付/支付宝商户**:在该接口对接支付下单 + 异步回调,回调成功后再发放会员。这步需要你的商户资质,代码里已留好发放会员的逻辑位。

## 已知限制 / 下一步
- **题目答案未提取**:`answer` 为空,故正确率显示「—」、判分提示「未录入」。下一步做"答案解析页提取"即可让判分/正确率全量可用。
- 资料分析跨页材料(选项图在下一页)等极端排版靠「待确认」人工关兜底。
- 多 worker 下后台入库任务建议后续上 Redis 队列(当前用进程内 BackgroundTasks)。
