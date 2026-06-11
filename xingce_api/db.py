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


class Question(Base):
    """题目核心表"""
    __tablename__ = "question"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(Integer, default=0)
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
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class WrongBook(Base):
    """错题本"""
    __tablename__ = "wrong_book"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, default=1, index=True)
    question_id: Mapped[int] = mapped_column(Integer, index=True)
    wrong_count: Mapped[int] = mapped_column(Integer, default=1)
    mastered: Mapped[int] = mapped_column(Integer, default=0)


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
    """用户-日 访问记录(算 UV/日活)"""
    __tablename__ = "visit_day"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    day: Mapped[str] = mapped_column(String(10), index=True)


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


def init_db():
    Base.metadata.create_all(engine)
    _migrate()
