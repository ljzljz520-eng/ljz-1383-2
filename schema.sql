PRAGMA foreign_keys = ON;

-- 用户：artist=作者(管理员) / client=客户
CREATE TABLE users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  role TEXT NOT NULL CHECK(role IN ('artist','client')),
  name TEXT NOT NULL,
  email TEXT UNIQUE
);

-- 委托入口：用途 + 交付范围；草稿态用不可枚举 token 恢复
CREATE TABLE commission_requests(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  client_id INTEGER REFERENCES users(id),
  contact_email TEXT,
  purpose TEXT NOT NULL DEFAULT '',          -- 用途
  delivery_scope TEXT NOT NULL DEFAULT '{}', -- JSON：尺寸/格式/数量/是否含源文件
  budget_cents INTEGER,
  desired_deadline TEXT,
  status TEXT NOT NULL DEFAULT 'draft'
    CHECK(status IN ('draft','submitted','quoted','accepted','rejected','converted')),
  draft_token TEXT UNIQUE,                   -- 提交后清空 => 恢复链接即失效
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  submitted_at TEXT
);

-- 项目：报价/修改次数/截止日/冻结模式/双方验收指针
CREATE TABLE projects(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  commission_id INTEGER REFERENCES commission_requests(id),
  title TEXT NOT NULL,
  confidential INTEGER NOT NULL DEFAULT 0,   -- 保密项目：硬覆盖一切公开授权
  freeze_mode TEXT NOT NULL DEFAULT 'stage' CHECK(freeze_mode IN ('project','stage')),
  quote_cents INTEGER NOT NULL DEFAULT 0,
  revision_limit INTEGER NOT NULL DEFAULT 0, -- 整包冻结模式下的全局修改额度
  revisions_used INTEGER NOT NULL DEFAULT 0,
  deadline TEXT,
  status TEXT NOT NULL DEFAULT 'active'
    CHECK(status IN ('active','delivered','accepted','closed','cancelled')),
  client_version_id INTEGER,                 -- 客户确认的版本
  artist_version_id INTEGER,                 -- 作者确认的版本
  accepted_version_id INTEGER,               -- 双方一致后落库
  lock_version INTEGER NOT NULL DEFAULT 0    -- 乐观锁
);

-- 阶段：草图→线稿→上色→成稿，逐阶段冻结与计次
CREATE TABLE stages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  kind TEXT NOT NULL CHECK(kind IN ('sketch','line','color','final')),
  seq INTEGER NOT NULL,
  frozen INTEGER NOT NULL DEFAULT 0,
  frozen_at TEXT,
  revision_limit INTEGER NOT NULL DEFAULT 0,
  revisions_used INTEGER NOT NULL DEFAULT 0,
  UNIQUE(project_id, kind)
);

-- 作品集条目
CREATE TABLE artworks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER REFERENCES projects(id),
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

-- 版本：不可变；批准后新增版本不会继承批准
CREATE TABLE artwork_versions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  artwork_id INTEGER NOT NULL REFERENCES artworks(id),
  stage_id INTEGER REFERENCES stages(id),
  seq INTEGER NOT NULL,
  file_hash TEXT NOT NULL,
  storage_path TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL,
  supersedes_version_id INTEGER REFERENCES artwork_versions(id),
  invalidated INTEGER NOT NULL DEFAULT 0,    -- 上游解冻返工 => 下游版本失效
  UNIQUE(artwork_id, seq)
);

-- 资产：原图与派生物（缩略图/过程图/网页图/水印图/成稿）分行授权
CREATE TABLE assets(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  version_id INTEGER NOT NULL REFERENCES artwork_versions(id),
  role TEXT NOT NULL CHECK(role IN ('original','final','process','web','thumbnail','watermark')),
  storage_path TEXT NOT NULL,
  file_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(version_id, role)
);

-- 权限矩阵：(资产 × 受众 × 动作) 一行一授权，默认拒绝；不存在"公开开关"
CREATE TABLE asset_permissions(
  asset_id INTEGER NOT NULL REFERENCES assets(id),
  audience TEXT NOT NULL CHECK(audience IN ('public','client','admin')),
  action TEXT NOT NULL CHECK(action IN ('display','download','index','share_card','relicense')),
  allowed INTEGER NOT NULL,
  PRIMARY KEY(asset_id, audience, action)
);

-- 批准：指向具体版本并快照内容哈希，永不迁移到新版本
CREATE TABLE approvals(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  stage_id INTEGER REFERENCES stages(id),
  version_id INTEGER NOT NULL REFERENCES artwork_versions(id),
  file_hash TEXT NOT NULL,
  approved_by INTEGER NOT NULL REFERENCES users(id),
  approved_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT ''
);

-- 批准后的变更差异
CREATE TABLE change_diffs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  from_version_id INTEGER NOT NULL,
  to_version_id INTEGER NOT NULL,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- 许可授权：可撤回；撤回是事件，触发权限与读模型重建
CREATE TABLE license_grants(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER REFERENCES projects(id),
  artwork_id INTEGER REFERENCES artworks(id),
  licensee TEXT NOT NULL,
  scope TEXT NOT NULL,        -- JSON array of actions
  terms TEXT NOT NULL DEFAULT '',
  granted_at TEXT NOT NULL,
  revoked_at TEXT,
  revoke_reason TEXT
);

-- 档期占位：held 带过期时间；确认是条件更新
CREATE TABLE schedule_holds(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slot_start TEXT NOT NULL,
  slot_end TEXT NOT NULL,
  project_id INTEGER REFERENCES projects(id),
  status TEXT NOT NULL DEFAULT 'held' CHECK(status IN ('held','booked','expired','released')),
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_holds_active ON schedule_holds(status, slot_start, slot_end);

-- 派生任务：幂等键 + 源哈希快照；晚到/失效任务不得复活旧内容
CREATE TABLE derivative_jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  version_id INTEGER NOT NULL REFERENCES artwork_versions(id),
  role TEXT NOT NULL,
  source_hash TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','running','done','superseded','failed')),
  idempotency_key TEXT UNIQUE,
  created_at TEXT NOT NULL,
  finished_at TEXT
);

-- 公开读模型：搜索/分享卡/精选的唯一数据源
CREATE TABLE public_listings(
  artwork_id INTEGER PRIMARY KEY REFERENCES artworks(id),
  title TEXT NOT NULL,
  summary TEXT NOT NULL,
  thumb_asset_id INTEGER,
  og_asset_id INTEGER,
  searchable_text TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE featured_items(
  artwork_id INTEGER PRIMARY KEY REFERENCES artworks(id),
  position INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

-- 下载包：内含"实际获批素材清单"
CREATE TABLE download_packages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  requested_by INTEGER NOT NULL REFERENCES users(id),
  manifest TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  entity TEXT NOT NULL,
  entity_id INTEGER,
  payload TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
