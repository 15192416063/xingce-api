# -*- coding: utf-8 -*-
"""数据库模型与会话(SQLAlchemy)。默认 SQLite,改 XC_DB_URL 即可切 MySQL。"""
from datetime import datetime
from sqlalchemy import (create_engine, Integer, String, Text, DateTime, func)
from sqlalchemy.orm import (DeclarativeBase, Mapped, mapped_column, sessionmaker)
import config

engine = create_engine(config.DB_URL, echo=False,
                       connect_args={"check_same_thread": False}
                       if config.DB_URL.startswith("sqlite") else {})
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

# SQLite 开 WAL:读写不再互斥(并发提升数倍),且断电崩溃不易损库(冗余性)
if config.DB_URL.startswith("sqlite"):
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _sqlite_pragma(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()


class Base(DeclarativeBase):
    pass


class IngestionJob(Base):
    """入库任务(状态机:0待处理 1解析中 2入库中 3完成 4失败)"""
    __tablename__ = "ingestion_job"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, default=0, index=True)  # 0=管理员公共入库
    file_name: Mapped[str] = mapped_column(String(255))
    file_path: Mapped[str] = mapped_column(String(512))
    scope: Mapped[str] = mapped_column(String(40), default="public")
    status: Mapped[int] = mapped_column(Integer, default=0)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    done_count: Mapped[int] = mapped_column(Integer, default=0)
    dup_count: Mapped[int] = mapped_column(Integer, default=0)
    graphic_count: Mapped[int] = mapped_column(Integer, default=0)
    missing_nums: Mapped[str] = mapped_column(String(255), default="")  # 切题缺号报告
    error_msg: Mapped[str] = mapped_column(String(1024), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class MaterialGroup(Base):
    """资料分析材料组:一段共享材料/图表,挂多道小题。"""
    __tablename__ = "material_group"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(Integer, default=0)
    scope: Mapped[str] = mapped_column(String(40), default="public", index=True)
    source: Mapped[str] = mapped_column(String(255), default="")
    material_text: Mapped[str] = mapped_column(Text, default="")
    # 该组图表(竖线分隔的多张相对路径)
    image_keys: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Paper(Base):
    """套卷:一份上传的 PDF = 一套卷。题库以套卷为单位展示/管理。"""
    __tablename__ = "paper"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, default=0, index=True)  # 0=公共(管理员)
    scope: Mapped[str] = mapped_column(String(40), default="public", index=True)
    title: Mapped[str] = mapped_column(String(255))
    source_file: Mapped[str] = mapped_column(String(255), default="")
    question_count: Mapped[int] = mapped_column(Integer, default=0)
    answer_count: Mapped[int] = mapped_column(Integer, default=0)   # 已录答案的题数
    status: Mapped[int] = mapped_column(Integer, default=1)         # 1正常 2已删
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Question(Base):
    """题目核心表"""
    __tablename__ = "question"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(Integer, default=0)
    paper_id: Mapped[int] = mapped_column(Integer, default=0, index=True)  # 所属套卷
    material_id: Mapped[int] = mapped_column(Integer, default=0, index=True)  # 资料分析关联材料组
    scope: Mapped[str] = mapped_column(String(40), default="public", index=True)
    source: Mapped[str] = mapped_column(String(255), default="")
    seq_no: Mapped[int] = mapped_column(Integer, default=0)
    category_l1: Mapped[str] = mapped_column(String(32), default="", index=True)
    category_l2: Mapped[str] = mapped_column(String(32), default="", index=True)
    knowledge_point: Mapped[str] = mapped_column(String(255), default="")
    topic_summary: Mapped[str] = mapped_column(String(512), default="")
    difficulty: Mapped[int] = mapped_column(Integer, default=2)  # 1易2中3难
    content: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(String(64), default="")
    answer_origin: Mapped[str] = mapped_column(String(8), default="")  # official/ai/空
    explanation: Mapped[str] = mapped_column(Text, default="")
    has_image: Mapped[int] = mapped_column(Integer, default=0)
    vector_id: Mapped[str] = mapped_column(String(64), default="")
    confidence: Mapped[int] = mapped_column(Integer, default=100)  # 0-100
    fingerprint: Mapped[str] = mapped_column(String(64), default="", index=True)
    # 1正常(进出题池) 0待确认 2已删
    status: Mapped[int] = mapped_column(Integer, default=1, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class QuestionImage(Base):
    """题目关联图(抠出的图)"""
    __tablename__ = "question_image"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    question_id: Mapped[int] = mapped_column(Integer, index=True)
    object_key: Mapped[str] = mapped_column(String(512))  # 相对 IMAGE_DIR 的路径
    seq: Mapped[int] = mapped_column(Integer, default=0)


class PracticeRecord(Base):
    """做题记录"""
    __tablename__ = "practice_record"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, default=1, index=True)
    question_id: Mapped[int] = mapped_column(Integer, index=True)
    user_answer: Mapped[str] = mapped_column(String(64), default="")
    is_correct: Mapped[int] = mapped_column(Integer, default=0)
    time_ms: Mapped[int] = mapped_column(Integer, default=0)   # 本题用时(毫秒,0=未记)
    error_tag: Mapped[str] = mapped_column(String(16), default="")  # 错因:概念不清/审题失误/计算错误/时间不够/蒙错
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class WrongExplain(Base):
    """错题讲解缓存:按(题, 用户所选错项)缓存 AI 三层讲解,同样的错法全员复用,省 token。
    同类提醒(历史错误次数)按用户本地拼接,不进缓存。"""
    __tablename__ = "wrong_explain"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    qid: Mapped[int] = mapped_column(Integer, index=True)
    user_answer: Mapped[str] = mapped_column(String(8), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    error_tag: Mapped[str] = mapped_column(String(16), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class MockExam(Base):
    """整卷模考记录(限时模拟+交卷出分)"""
    __tablename__ = "mock_exam"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    paper_id: Mapped[int] = mapped_column(Integer, index=True)
    time_limit_min: Mapped[int] = mapped_column(Integer, default=120)
    submitted: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)       # 卷面题数
    answered: Mapped[int] = mapped_column(Integer, default=0)
    correct: Mapped[int] = mapped_column(Integer, default=0)
    time_used_sec: Mapped[int] = mapped_column(Integer, default=0)
    report: Mapped[str] = mapped_column(Text, default="")        # 分模块统计(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class WrongBook(Base):
    """错题本(带间隔重复:做对一次进下一档 1/3/7/15 天,四档全过自动掌握)"""
    __tablename__ = "wrong_book"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, default=1, index=True)
    question_id: Mapped[int] = mapped_column(Integer, index=True)
    wrong_count: Mapped[int] = mapped_column(Integer, default=1)
    mastered: Mapped[int] = mapped_column(Integer, default=0)
    box: Mapped[int] = mapped_column(Integer, default=0)           # 复习档位
    next_review: Mapped[str] = mapped_column(String(10), default="")  # YYYY-MM-DD


class Favorite(Base):
    """收藏夹"""
    __tablename__ = "favorite"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, default=1, index=True)
    question_id: Mapped[int] = mapped_column(Integer, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class User(Base):
    """用户账号(密码只存哈希,绝不存明文)"""
    __tablename__ = "user"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    nickname: Mapped[str] = mapped_column(String(64), default="")
    role: Mapped[int] = mapped_column(Integer, default=0)        # 0普通 1管理员
    status: Mapped[int] = mapped_column(Integer, default=1)      # 1正常 0封禁
    # 令牌版本:登录/改密时+1,旧令牌立即全部失效(单设备登录/吊销的核心)
    token_ver: Mapped[int] = mapped_column(Integer, default=0)
    invite_code: Mapped[str] = mapped_column(String(32), default="")  # 注册时用的邀请码
    source: Mapped[str] = mapped_column(String(40), default="")  # 注册来源/渠道:xhs/zhihu/share/direct
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class InviteCode(Base):
    """邀请码:同一码可限制注册人数,管理面板生成/停用。"""
    __tablename__ = "invite_code"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    max_uses: Mapped[int] = mapped_column(Integer, default=10)
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    note: Mapped[str] = mapped_column(String(128), default="")
    created_by: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ExamInfo(Base):
    """全国考试日历:各省/国考的报名与笔试时间(内置+自动抓取+手动维护)。"""
    __tablename__ = "exam_info"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    region: Mapped[str] = mapped_column(String(32), index=True)      # 全国/北京/广东…
    exam_type: Mapped[str] = mapped_column(String(32), default="省考")  # 国考/省考/事业单位/选调生
    name: Mapped[str] = mapped_column(String(128))
    signup_start: Mapped[str] = mapped_column(String(20), default="")  # YYYY-MM-DD,未知留空
    signup_end: Mapped[str] = mapped_column(String(20), default="")
    exam_date: Mapped[str] = mapped_column(String(20), default="")
    announce_url: Mapped[str] = mapped_column(String(512), default="")
    note: Mapped[str] = mapped_column(String(255), default="")
    origin: Mapped[str] = mapped_column(String(16), default="内置")   # 内置/抓取/手动
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AiChannel(Base):
    """AI 模型渠道(多个 OpenAI 兼容 API,按优先级自动故障切换)"""
    __tablename__ = "ai_channel"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64))                # 显示名称,如 DeepSeek
    base_url: Mapped[str] = mapped_column(String(255))           # 如 https://api.deepseek.com/v1
    api_key: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(128))              # 如 deepseek-chat
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    priority: Mapped[int] = mapped_column(Integer, default=10)   # 小的先用
    supports_vision: Mapped[int] = mapped_column(Integer, default=0)  # 1=能看图(OCR/图形题)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)  # 累计失败(监控用)
    last_error: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Setting(Base):
    """运行时配置(管理员面板改 API key 等,优先级高于环境变量)"""
    __tablename__ = "setting"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    skey: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    sval: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AuditLog(Base):
    """管理员操作审计(防篡改:谁在什么时候改了什么,有据可查)"""
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    action: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class LoginLog(Base):
    """登录审计:成功/失败都记,管理员可见(发现爆破/盗号)"""
    __tablename__ = "login_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), index=True)
    ip: Mapped[str] = mapped_column(String(64), default="")
    ok: Mapped[int] = mapped_column(Integer, default=0)   # 1成功 0失败
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AiUsage(Base):
    """每用户每日 AI 调用计数(防滥用/控成本)"""
    __tablename__ = "ai_usage"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    day: Mapped[str] = mapped_column(String(10), index=True)   # YYYY-MM-DD
    count: Mapped[int] = mapped_column(Integer, default=0)


class StatDaily(Base):
    """每日运营统计:PV/对话次数/token 消耗(管理员面板用)"""
    __tablename__ = "stat_daily"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    day: Mapped[str] = mapped_column(String(10), unique=True, index=True)  # YYYY-MM-DD
    pv: Mapped[int] = mapped_column(Integer, default=0)            # 页面打开人次
    chat_count: Mapped[int] = mapped_column(Integer, default=0)    # AI 对话次数
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)     # LLM 输入 token
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)    # LLM 输出 token


class VisitDay(Base):
    """用户-日 访问记录(算 UV/日活;dwell_sec 累计当日停留秒数,前端心跳上报)"""
    __tablename__ = "visit_day"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    day: Mapped[str] = mapped_column(String(10), index=True)
    dwell_sec: Mapped[int] = mapped_column(Integer, default=0)   # 当日累计停留秒数


class UserMemory(Base):
    """每用户的"学习档案"(类 Claude memory):AI 逐步了解到的用户情况,
    每次对话注入个性化,定期由 AI 合并更新。content 为 Markdown。"""
    __tablename__ = "user_memory"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    content: Mapped[str] = mapped_column(Text, default="")   # Markdown 档案
    turns: Mapped[int] = mapped_column(Integer, default=0)   # 对话轮数(用于节流更新)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Feedback(Base):
    """用户反馈:使用中遇到的问题/建议。后台可见,可选发邮件给管理员。"""
    __tablename__ = "feedback"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    username: Mapped[str] = mapped_column(String(64), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    contact: Mapped[str] = mapped_column(String(128), default="")  # 用户留的联系方式(选填)
    status: Mapped[int] = mapped_column(Integer, default=0)        # 0未读 1已读
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ChatLog(Base):
    """AI 对话流水:每次对话存一条。供后台审计「聊了什么/粘了什么题」+ 点赞点踩。"""
    __tablename__ = "chat_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    message: Mapped[str] = mapped_column(Text, default="")      # 用户输入
    reply: Mapped[str] = mapped_column(Text, default="")        # AI 回复
    category: Mapped[str] = mapped_column(String(64), default="")  # 识别到的题型
    is_paste: Mapped[int] = mapped_column(Integer, default=0)   # 1=粘贴了完整题目
    feedback: Mapped[int] = mapped_column(Integer, default=0)   # 1赞 / -1踩 / 0未评
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class TokenStat(Base):
    """Token 分账:按 (日, 用途场景, 渠道) 聚合,运营面板看「钱花在哪」。
    scene: 对话/AI解题/题目分类/批量入库分类/PDF切题/答案解析/扫描OCR/向量检索 等
    channel: 命中的渠道名(如 DeepSeek、硅基流动-bge);.env 兜底记为「DeepSeek(.env)」"""
    __tablename__ = "token_stat"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    day: Mapped[str] = mapped_column(String(10), index=True)       # YYYY-MM-DD
    scene: Mapped[str] = mapped_column(String(32), default="其他", index=True)
    channel: Mapped[str] = mapped_column(String(64), default="")
    calls: Mapped[int] = mapped_column(Integer, default=0)         # 调用次数
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)


class News(Base):
    """考试资讯/公告(管理员发布,用户可见)"""
    __tablename__ = "news"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(String(400), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(32), default="资讯")  # 公告/资讯/考试时间
    url: Mapped[str] = mapped_column(String(512), default="")
    pinned: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class QuestionChat(Base):
    """针对某道题的"追问线程":每用户每题一条独立会话,可多轮追问、可回看历史。
    role: user/assistant。AI 答疑时始终带着这道题的题干/答案/解析作上下文。"""
    __tablename__ = "question_chat"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    question_id: Mapped[int] = mapped_column(Integer, index=True)
    role: Mapped[str] = mapped_column(String(16), default="user")  # user / assistant
    content: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ExplainEvent(Base):
    """解析触发事件:用户对某题请求/查看 AI 解析记一条(冷启动观测"核心功能使用信号")。
    kind: solve(生成解析/AI解答) / wrong(为什么错·错题讲解)。cached=1 表示命中缓存。"""
    __tablename__ = "explain_event"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    question_id: Mapped[int] = mapped_column(Integer, index=True)
    qtype: Mapped[str] = mapped_column(String(32), default="")   # 二级题型(category_l2)
    kind: Mapped[str] = mapped_column(String(16), default="solve")
    cached: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ExplainFeedback(Base):
    """解析反馈(质量监控金矿):用户对某条 AI 解析点"有用/没用/报错"。
    rating: useful / useless / error。同(用户,题,kind)只保留最新一条(前端覆盖)。"""
    __tablename__ = "explain_feedback"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    question_id: Mapped[int] = mapped_column(Integer, index=True)
    qtype: Mapped[str] = mapped_column(String(32), default="")
    kind: Mapped[str] = mapped_column(String(16), default="solve")
    rating: Mapped[str] = mapped_column(String(16), default="")   # useful/useless/error
    text: Mapped[str] = mapped_column(Text, default="")           # 可选文字反馈
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class GuidanceLog(Base):
    """AI 引导式思维拆解流水:每次 generate_guidance 记一条(便于排查 + 攒守护层样本)。
    guard_triggered=1 表示生成文本疑似直接给了答案(守护层被突破),供后续调 prompt 用。"""
    __tablename__ = "guidance_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    question_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    retrieved_methods: Mapped[str] = mapped_column(String(255), default="")  # 命中方法论ID,逗号分隔
    guidance_text: Mapped[str] = mapped_column(Text, default="")
    guard_triggered: Mapped[int] = mapped_column(Integer, default=0, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class StuckRecord(Base):
    """卡点沉淀(本期只写不读,为将来「卡点→画像→薄弱点推荐」闭环攒数据)。
    关键是 point_id(知识点类型,能泛化到推荐),而非"第几号题卡过"。"""
    __tablename__ = "stuck_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    question_id: Mapped[int] = mapped_column(Integer, index=True)
    module: Mapped[str] = mapped_column(String(32), default="", index=True)
    question_type: Mapped[str] = mapped_column(String(64), default="")
    point_id: Mapped[str] = mapped_column(String(64), default="", index=True)  # 知识点ID,泛化到推荐的关键
    point_label: Mapped[str] = mapped_column(String(128), default="")
    step_index: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(16), default="")  # preset / free_text
    raw_text: Mapped[str] = mapped_column(Text, default="")      # 自由输入原文;预设为空
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class GuidanceCache(Base):
    """AI 引导分步结果缓存(按题共享,像解析一样全员复用):同一题秒出且每次一致,
    省 token。steps 存 JSON;质量不好时可「重新生成」覆盖本行。"""
    __tablename__ = "guidance_cache"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    question_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    steps_json: Mapped[str] = mapped_column(Text, default="")        # [{tag,title,body,point_id,point_label}]
    method_ids: Mapped[str] = mapped_column(String(255), default="")
    guard_triggered: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


def _migrate():
    """轻量自动迁移(SQLite):给已存在的表补上模型新增的列,避免旧库缺列报错。
    生产用 MySQL 时请改用正式迁移工具(Alembic)。"""
    if not config.DB_URL.startswith("sqlite"):
        return
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in tables:
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            coltype = col.type.compile(engine.dialect)
            default = ""
            d = getattr(col.default, "arg", None) if col.default is not None else None
            if isinstance(d, (int, float)):
                default = f" DEFAULT {d}"
            elif isinstance(d, str):
                default = " DEFAULT '%s'" % d.replace("'", "''")
            try:
                with engine.begin() as conn:
                    conn.execute(text(
                        f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {coltype}{default}'))
            except Exception:
                pass
    # 旧版会员体系遗留的 NOT NULL 列(新模型已删除):
    # INSERT 不再提供这些列的值,不删掉会导致注册报 NOT NULL constraint failed
    legacy = {"user": ("membership_level", "membership_expire")}
    for tname, cols in legacy.items():
        if tname not in tables:
            continue
        have = {c["name"] for c in insp.get_columns(tname)}
        model_cols = {c.name for c in Base.metadata.tables[tname].columns} \
            if tname in Base.metadata.tables else set()
        for cname in cols:
            if cname in have and cname not in model_cols:
                try:
                    with engine.begin() as conn:
                        conn.execute(text(
                            f'ALTER TABLE "{tname}" DROP COLUMN "{cname}"'))
                except Exception:
                    pass


def _backfill_papers():
    """老库迁移:paper_id=0 的存量题,按 (scope, source) 归组补建套卷。幂等。"""
    db = SessionLocal()
    try:
        rows = (db.query(Question.scope, Question.source)
                .filter(Question.paper_id == 0, Question.status != 2)
                .distinct().all())
        for scope, source in rows:
            uid = int(scope[5:]) if scope.startswith("user:") else 0
            title = (source or "未命名卷").rsplit(".", 1)[0][:255]
            p = db.query(Paper).filter(Paper.scope == scope,
                                       Paper.source_file == (source or "")).first()
            if not p:
                p = Paper(user_id=uid, scope=scope, title=title,
                          source_file=source or "")
                db.add(p)
                db.commit()
            db.query(Question).filter(Question.paper_id == 0,
                                      Question.scope == scope,
                                      Question.source == (source or "")) \
                .update({Question.paper_id: p.id}, synchronize_session=False)
            db.commit()
        # 重算每卷题数/答案数
        for p in db.query(Paper).filter(Paper.status == 1).all():
            qs = db.query(Question).filter(Question.paper_id == p.id,
                                           Question.status == 1)
            p.question_count = qs.count()
            p.answer_count = qs.filter(Question.answer != "").count()
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(engine)
    _migrate()
    _backfill_papers()
