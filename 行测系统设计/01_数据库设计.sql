-- ============================================================
-- 行测智能题库 SaaS — MySQL 表结构设计 (MySQL 8.0+, utf8mb4)
-- 设计原则:
--   1. 私有库优先:每条题带 scope 字段做隔离与检索过滤(后端强校验)
--   2. 贵操作(分类/解析/向量/多模态)入库时一次性算清,结果落库
--   3. 入库走任务表 + 状态机,保证可重试、可断点续、不重复扣费
--   4. 向量本体存 Redis Stack,这里只存 vector_id 引用
-- ============================================================
SET NAMES utf8mb4;

-- ---------- 用户 ----------
CREATE TABLE `user` (
  `id`         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `openid`     VARCHAR(64)  NULL COMMENT '微信openid',
  `nickname`   VARCHAR(64)  NULL,
  `avatar`     VARCHAR(512) NULL,
  `phone`      VARCHAR(20)  NULL,
  `role`       TINYINT      NOT NULL DEFAULT 0 COMMENT '0普通 1管理员',
  `status`     TINYINT      NOT NULL DEFAULT 1 COMMENT '1正常 0封禁',
  `created_at` DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_openid` (`openid`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户';

-- ---------- 会员/额度 ----------
-- 实时额度计数放 Redis(扣减原子性);这里是持久化底账,Redis 重建时回灌
CREATE TABLE `membership` (
  `id`               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `user_id`          BIGINT UNSIGNED NOT NULL,
  `level`            TINYINT      NOT NULL DEFAULT 0 COMMENT '0免费 1月度 2年度',
  `expire_at`        DATETIME     NULL COMMENT '会员到期时间',
  `upload_quota`     INT          NOT NULL DEFAULT 200  COMMENT '剩余可上传题数',
  `multimodal_quota` INT          NOT NULL DEFAULT 20   COMMENT '剩余图推/多模态额度',
  `updated_at`       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_user` (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='会员与额度';

-- ---------- 题库 ----------
-- owner_user_id 为 NULL = 公共题库(scope=public);否则为某用户私有库
CREATE TABLE `question_bank` (
  `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `owner_user_id` BIGINT UNSIGNED NULL COMMENT 'NULL=公共库',
  `name`          VARCHAR(128) NOT NULL,
  `scope`         VARCHAR(40)  NOT NULL COMMENT 'public 或 user:{id}',
  `question_count`INT          NOT NULL DEFAULT 0,
  `created_at`    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_owner` (`owner_user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='题库';

-- ---------- 资料分析材料组(共享材料,一组带多道小题) ----------
CREATE TABLE `material_group` (
  `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `bank_id`       BIGINT UNSIGNED NOT NULL,
  `scope`         VARCHAR(40)  NOT NULL,
  `material_text` MEDIUMTEXT   NOT NULL COMMENT '共享材料原文',
  `source`        VARCHAR(255) NULL,
  `created_at`    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_bank` (`bank_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='资料分析材料组';

-- ---------- 题目(核心表) ----------
CREATE TABLE `question` (
  `id`              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `bank_id`         BIGINT UNSIGNED NOT NULL,
  `scope`           VARCHAR(40)  NOT NULL COMMENT '冗余,检索过滤用,必建索引',
  `source`          VARCHAR(255) NULL COMMENT '来源卷子名',
  `seq_no`          INT          NULL COMMENT '原卷题号',
  -- 行测题型分类(这是"按类型出题"的命脉)
  `category_l1`     VARCHAR(32)  NULL COMMENT '一级:言语理解/数量关系/判断推理/资料分析/常识判断/政治理论',
  `category_l2`     VARCHAR(32)  NULL COMMENT '二级:逻辑填空/片段阅读/图形推理/定义判断/类比推理/逻辑判断/...',
  `knowledge_point` VARCHAR(255) NULL COMMENT '考点',
  `topic_summary`   VARCHAR(512) NULL COMMENT '考点摘要(算向量的来源,也展示)',
  `difficulty`      TINYINT      NULL COMMENT '1易 2中 3难',
  -- 内容
  `content`         MEDIUMTEXT   NOT NULL COMMENT '题干+选项原文',
  `question_type`   TINYINT      NOT NULL DEFAULT 0 COMMENT '0单选 1多选',
  `answer`          VARCHAR(64)  NULL,
  `explanation`     MEDIUMTEXT   NULL,
  `answer_source`   TINYINT      NOT NULL DEFAULT 0 COMMENT '0原卷 1AI生成(需标注仅供参考)',
  -- 关联
  `material_id`     BIGINT UNSIGNED NULL COMMENT '资料分析关联材料组,否则NULL',
  `has_image`       TINYINT      NOT NULL DEFAULT 0 COMMENT '图推/资料分析图表',
  `vector_id`       VARCHAR(64)  NULL COMMENT 'Redis 向量库 key',
  -- 质量与去重
  `confidence`      TINYINT      NOT NULL DEFAULT 100 COMMENT '分类置信度0-100,低的待人工确认',
  `fingerprint`     VARCHAR(64)  NULL COMMENT '内容指纹,去重用',
  `status`          TINYINT      NOT NULL DEFAULT 1 COMMENT '1正常 0待确认 2已删',
  `created_at`      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_serve` (`scope`,`category_l1`,`category_l2`,`difficulty`,`status`) COMMENT '按题型出题的核心索引',
  KEY `idx_bank` (`bank_id`),
  KEY `idx_fp` (`fingerprint`),
  KEY `idx_material` (`material_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='题目';

-- ---------- 图片(图推选项图 / 资料分析图表 / 整页图) ----------
CREATE TABLE `question_image` (
  `id`         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `ref_type`   TINYINT      NOT NULL COMMENT '0题目 1材料组',
  `ref_id`     BIGINT UNSIGNED NOT NULL COMMENT 'question_id 或 material_id',
  `img_type`   TINYINT      NOT NULL COMMENT '0整页 1裁剪图表 2选项图',
  `object_key` VARCHAR(512) NOT NULL COMMENT 'MinIO 对象key',
  `seq`        INT          NOT NULL DEFAULT 0,
  `created_at` DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_ref` (`ref_type`,`ref_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='题目/材料关联图片';

-- ---------- 做题记录 ----------
CREATE TABLE `practice_record` (
  `id`          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `user_id`     BIGINT UNSIGNED NOT NULL,
  `question_id` BIGINT UNSIGNED NOT NULL,
  `user_answer` VARCHAR(64)  NULL,
  `is_correct`  TINYINT      NOT NULL COMMENT '0错 1对',
  `duration_ms` INT          NULL COMMENT '作答耗时',
  `created_at`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_user_q` (`user_id`,`question_id`),
  KEY `idx_user_time` (`user_id`,`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='做题记录(统计/避免重复出题)';

-- ---------- 错题本 ----------
CREATE TABLE `wrong_book` (
  `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `user_id`       BIGINT UNSIGNED NOT NULL,
  `question_id`   BIGINT UNSIGNED NOT NULL,
  `wrong_count`   INT          NOT NULL DEFAULT 1,
  `last_wrong_at` DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `mastered`      TINYINT      NOT NULL DEFAULT 0 COMMENT '1已掌握(移出复习队列)',
  `created_at`    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_user_q` (`user_id`,`question_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='错题本';

-- ---------- 入库任务(状态机:可重试/断点续/进度回传) ----------
CREATE TABLE `ingestion_job` (
  `id`              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `user_id`         BIGINT UNSIGNED NOT NULL,
  `bank_id`         BIGINT UNSIGNED NULL,
  `file_name`       VARCHAR(255) NOT NULL,
  `file_object_key` VARCHAR(512) NOT NULL COMMENT 'MinIO 原始PDF',
  `status`          TINYINT      NOT NULL DEFAULT 0 COMMENT '0待处理 1解析中 2入库中 3完成 4失败',
  `progress`        INT          NOT NULL DEFAULT 0 COMMENT '0-100',
  `total_count`     INT          NOT NULL DEFAULT 0,
  `done_count`      INT          NOT NULL DEFAULT 0,
  `dup_count`       INT          NOT NULL DEFAULT 0 COMMENT '去重跳过数',
  `graphic_count`   INT          NOT NULL DEFAULT 0 COMMENT '图推题数(多模态额度统计)',
  `retry_count`     TINYINT      NOT NULL DEFAULT 0,
  `error_msg`       VARCHAR(1024) NULL,
  `created_at`      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `finished_at`     DATETIME     NULL,
  PRIMARY KEY (`id`),
  KEY `idx_user` (`user_id`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='入库任务';

-- ============================================================
-- 说明:
-- · "给我XX题"出题 = 走 idx_serve 索引的纯 SQL 筛选,毫秒级、零AI成本:
--     SELECT * FROM question
--     WHERE scope IN ('public', CONCAT('user:', ?))   -- 公共库+本人私有库
--       AND category_l2 = '图形推理' AND status = 1
--       AND id NOT IN (本人已做题id)                    -- 避免重复(可用 practice_record 反连接)
--     ORDER BY difficulty LIMIT 10;
-- · "找相似题/同类强化" = 拿该题 topic_summary 的向量去 Redis Stack 检索,再回表。
-- · scope 过滤必须在后端 SQL 里做,绝不能信前端传的范围(私有隔离=隐私红线)。
-- ============================================================
