# 青年插画师工作室平台

面向青年插画师的作品集 + 委托 + 项目交付一体化系统：

- **公开 Web 作品集**：展示「草图 → 过程图 → 成稿」的创作过程，搜索、首页精选、分享卡。
- **委托入口**：收集用途、交付范围、联系、预算；草稿仅能在原浏览器恢复。
- **管理后台 + 关系库 + 后台 API**：作品版本、逐素材授权、项目排期、两种冻结模式、
  报价/修改次数/截止日约束、审批快照、两方验收、可追溯交付包。

## 技术栈

FastAPI · SQLAlchemy 2.x（关系库，默认 SQLite，可换 PostgreSQL/MySQL）·
Pillow（异步图片派生）· 标准库 `zipfile`（交付包）· 原生 HTML/JS 前端（无构建步骤）·
pytest/httpx（20 个端到端测试）。

## 快速开始

```bash
python3 -m venv --without-pip .venv          # 本机无 ensurepip 时
.venv/bin/python get-pip.py                  # 如已带 pip 可跳过
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload --port 8000
# 打开 http://127.0.0.1:8000/
```

默认管理员 `admin / studio2026`（可用 `ADMIN_USER`/`ADMIN_PASSWORD` 环境变量覆盖）。
进入 `admin.html` 登录后点 **「一键生成演示数据」**，会创建艺术家、客户、
公开作品（成稿仅展示、草图明确拒绝展示、缩略图异步派生且默认无权）、保密作品
（即使打了精选标记也不公开）、一个逐阶段冻结的委托项目。客户门户令牌会在
「人员/客户令牌」面板中给出 capability URL。

## 目录

```
app/
  models.py              # 关系模型：作品/版本/不可变素材/授权/审批/验收/交付/排期/委托
  routers/               # auth, public, drafts, client, admin_works/projects/people/trace, seed
  services/
    rights.py            # 逐素材 × 逐权利（展示/下载/再授权）× 作用域 判定，默认拒绝
    storage.py           # 内容寻址(SHA256)的不可变文件存储
    images.py            # 异步派生队列（缩略图/过程图），产出不自动继承授权
    revisions.py         # 整包/逐阶段冻结 + 报价/次数/截止日 + 变更单
    schedule.py          # 档期占位 TTL 过期、预约冲突
    approvals.py         # 批准快照与漂移检测、两方同清单验收
    delivery.py          # 封包清单=实际验收素材 + 授权快照、撤回后阻断下载
    catalog.py           # 公开读取：精选/搜索/分享卡，实时复核授权与保密
web/                     # 公开站 / 委托页 / 客户门户 / 管理后台
tests/                   # 4 个测试文件，20 个用例
```

## 关键设计：为什么不是「一个公开开关」

| 需求风险 | 设计 |
|---|---|
| 同一图像有展示权却无下载/再授权权 | `LicenseGrant(asset, scope, right, granted)`：权利三维独立；`display/download/sublicense` 分别授权或拒绝，默认无任何权利 |
| 缩略图/过程图/成稿不能共用开关 | 它们是 `FileAsset` 的不同行（`kind=master/process/thumbnail`），授权逐行生效；派生任务产出**不继承**源素材任何授权 |
| 授权可撤回 | `revoked_at` 置位后公开读、搜索、分享卡、交付下载立即生效；历史封包清单仍保留用于追溯 |
| 客户批准后作者又改稿 | 批准写入 `VersionApproval.asset_snapshot/fingerprint`（每个素材 sha256 的快照）。新上传只产生新素材，接口显式返回 `drift.changed=true`，**绝不把最新文件标成已批准** |
| 两方确认不同版本 | 验收单带 `items_fingerprint`；任一方提交的清单与建单清单不一致直接 422，两方都确认同一份才算完成 |
| 下载包必须是实际获批素材 | 封包只取验收单里的 `(version_id, asset_id, sha256)`，逐字节校验；ZIP 内含 `MANIFEST.json`（素材清单+交付时授权快照） |
| 整包冻结 vs 逐阶段冻结 | `Project.freeze_mode=package/stage`；整包模式拒绝单阶段冻结并锁定全部阶段；逐阶段模式只锁具体阶段，其余仍可改 |
| 报价/修改次数/截止日约束修订 | `revisions.evaluate` 给出 blockers/warnings：冻结=拒绝；次数用尽或逾期=必须走 `ChangeOrder`（追加报价、追加次数、新截止日），客户接受后才执行并消耗一次修改 |
| 档期占位过期 | `ScheduleSlot.hold_expires_at`；列表/预约时惰性过期，过期占位不能预约；已预约区间有重叠冲突检测 |
| 保密项目误入首页精选 | 读取路径同时要求 `confidential=false` **且** 至少一个具体素材持有有效公开展示授权；搜索索引是加速层，读取时仍实时复核 |
| 派生任务晚到 | 晚完成的派生图是独立素材：无授权则不上公开站，也不在已封包的清单/zip 中；`source_asset_id` 保留血缘 |
| 公开搜索/分享卡只读允许版本 | 搜索命中后重新跑 `public_story` 授权复核；分享卡必须存在持展示权的缩略图，否则 404 |
| 委托草稿串浏览器 | 随机 `draft_token` + HttpOnly Cookie 双重提交校验；无枚举接口；后台只看得到**已提交**内容，未提交仅返回计数 |
| 管理可追溯审批与交付依赖 | `/api/admin/trace/projects/{id}/delivery`：批准 → 验收项 → 封包项的 sha256 链；另有全量审计时间线 |

## API 速览

- 公开（匿名）：`GET /api/public/featured|works|works/{id}/story|search|share/{id}`，
  `GET /api/public/files/{asset_id}/bytes`（仅有效公开展示权，且从不据此发放下载）。
- 委托：`POST /api/commission/drafts`、`GET /drafts/restore`、`POST /drafts/submit`。
- 客户门户（`X-Client-Token` 头或 Cookie）：批准版本、批准漂移查询、发起/两方确认验收、
  封包、列包、下载 ZIP。
- 管理后台（签名 Cookie 会话）：作品/版本/上传、授权设置与撤回、派生队列、
  项目/阶段/冻结、修订与变更单、档期、审批与交付追溯、审计、已提交委托。

## 测试

```bash
.venv/bin/python -m pytest -q
# 20 passed
```

覆盖：默认拒绝与部分授权、撤回即时生效、显式拒绝优先、派生图独立授权、
批准后改稿不污染批准对象、两方异版验收被拒、封包清单=获批 sha 且 ZIP 校验、
封包后撤回阻断下载、两种冻结、次数/逾期变更单、占位过期与预约冲突、
保密精选不公开、分享卡门槛、草稿跨浏览器隔离、晚到派生不进交付。

## 备注 / 生产化建议

- 当前客户门户用 capability token 演示「客户」身份，作者确认方在演示中同入口记录；
  生产环境应为作者增加独立登录并对封包动作加双方鉴权。
- 派队列是进程内同步处理器（`POST /api/admin/derivations/run`），可替换为 Celery/RQ。
- 文件存储为本地内容寻址目录，可替换 S3；`sha256` 已作为业务键贯通。
