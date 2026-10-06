# 青年插画师作品集与委托管理平台

Web 展示草图→成稿全过程的作品集 + 委托入口 + 后台管理。纯标准库实现
（Python 3.11 / sqlite3 / http.server），零依赖可运行。

```
schema.sql          关系库 DDL（权限矩阵、版本、批准、冻结、档期、派生任务…）
app/
  db.py             连接与事务（BEGIN IMMEDIATE 写事务）
  permissions.py    资产×受众×动作 权限矩阵，读取时解析有效权限
  versions.py       不可变版本、批准（钉住版本+哈希）、批准后差异自动登记
  freezing.py       整包/逐阶段冻结、修订额度原子扣减、追加报价
  projects.py       项目创建、双方验收（乐观锁+一致才成交）、保密切换
  scheduling.py     档期占位（held→booked，过期即失效）
  derivatives.py    派生任务管线（幂等、晚到守卫、授权守卫）
  licensing.py      许可授权与撤回（撤回=事件，联动权限/读模型/任务）
  public.py         公开读模型：搜索、分享卡、首页精选的唯一数据源
  drafts.py         委托草稿 capability-token 隔离
  packages.py       下载包：只含实际获批素材的清单
  audit.py          审计与项目追溯（审批链、差异、交付依赖）
  api.py            JSON API（stdlib http.server）
tests/              27 个用例覆盖全部关键场景
```

## 一、权限：为什么不能用一个"公开开关"

同一图像可能**有公开展示权、却无下载权、更无再授权权**；它的缩略图、过程图、
成稿是不同资产，授权面各不相同（缩略图可公开索引、过程图仅客户可见、成稿可
展示不可下载）。因此权限模型是矩阵而不是布尔：

```
asset_permissions(asset_id, audience, action, allowed)
  asset   : 原图/成稿/过程图/网页图/缩略图/水印图 —— 每个派生物独立成行
  audience: public / client / admin
  action  : display / download / index / share_card / relicense
```

有效权限在**读取时**解析（`permissions.effective`）：
管理员放行 → 保密项目对非管理员**硬覆盖** → 显式授权行 → 默认拒绝。
任何派生任务、缓存、读模型都不烘焙权限结论，所以授权撤回即刻生效。

## 二、版本与批准：批准对象永不漂移

- `artwork_versions` 不可变，只追加；新版本记录 `supersedes_version_id` 成链。
- `approvals` 写入 `(version_id, file_hash)` 快照。**客户批准的是那个版本的内容，
  不是"这个作品的最新文件"**——作者批准后再上传 v3，批准仍钉在 v2，
  `approval_status` 返回 `stale=true`。
- 批准后一旦出现新版本，自动写入 `change_diffs`（从哪版到哪版、哈希变化、备注），
  管理页可逐条追溯。
- 下载包清单以批准记录为准（见第六节），最新未获批文件永远不会混进交付。

## 三、冻结策略：整包冻结 vs 逐阶段冻结

| 维度 | 按项目整包冻结 | 逐阶段冻结（草图/线稿/上色/成稿） |
|---|---|---|
| 计数 | 项目级一个修改额度 | 每阶段独立额度与计价 |
| 解冻影响面 | 一次修订解冻全部阶段，已稳成果暴露于改动 | 只解冻目标阶段，下游成果级联作废（`invalidated`）待重做 |
| 并行度 | 低：冻结期内无法推进任何后续工作 | 高：下游可基于已冻结的上游放心开工 |
| 超支定位 | 全局计数，超支难归因到环节 | 超支精确到阶段，追加报价按阶段核算 |
| 截止日 | 单一总截止 | 阶段里程碑 + 总截止兜底 |
| 实现成本 | 低 | 需要依赖级联与失效传播 |
| 适用 | 小单、快单、一次性交付 | 多阶段商业委托 |

**落地建议**：默认逐阶段冻结，项目级保留总截止日与总预算作护栏。
两种模式在本系统中并存（`projects.freeze_mode`），修订约束的落实点相同：

1. **修改次数**：`UPDATE … SET revisions_used=revisions_used+1
   WHERE revisions_used<revision_limit` 原子扣减；额度用尽返回 **402**，
   必须走 `purchase_amendment`（追加报价：提额 + 计入报价 + 审计留痕）才能继续。
2. **截止日**：`now > deadline` 的修订请求直接 **409**，须先重排档期与报价。
3. **未冻结阶段**的改动不计修订（**400**），防止额度被无谓消耗。

## 四、并发与边界场景

**验收两方同时确认不同版本** —— 双方各自确认的版本写入
`client_version_id / artist_version_id`（乐观锁条件更新），**一致才成交**；
不一致进入 `mismatch` 状态，任何一方都无法单方面把"最新文件"写成验收结果；
已验收后再确认返回 **409**。

**档期占位过期** —— 占位是 `held` + `expires_at`；确认是条件更新
`…WHERE status='held' AND expires_at>now`，过期占位确认返回 **410** 并落
`expired`；重叠时段在事务内拒绝（**409**）；另有 `sweep_expired` 定期清扫。

**保密项目误加入首页精选** —— 三道防线：写守卫（不在公开读模型中的作品拒绝
加入精选，**403**）；保密切换是事件，立即重建读模型并清除精选行；首页读取时
`JOIN public_listings` 再过滤一次，脏数据也不外泄。

**图片派生任务晚到** —— 任务携带幂等键 + 源哈希快照入队；执行前校验：源版本
已被更新版本取代/作废 → `superseded`；授权已撤回 → `superseded-license-revoked`；
且权限在读取时解析，晚到任务即使完成也无法让撤回的内容重新公开。

**授权撤回** —— 撤回是事件而非改一个字段：写拒绝行（public/client 全动作）→
清公开读模型与精选 → 未完成的派生任务置失效 → 审计留痕。已交付的历史下载由
合同条款约束，新下载与公开展示即刻关闭。

## 五、公开站与委托入口

- **搜索 / 分享卡 / 首页精选**只读 `public_listings` 投影表，绝不直查资产表；
  投影由权限/保密/授权变化触发重建，天然只含"允许公开的版本"。
- **委托草稿恢复**：创建草稿返回 256 位随机 token，仅存于填写者浏览器的
  localStorage；服务端无枚举接口、无顺序 id；提交即失效、过期清扫——
  其他浏览器用户无法恢复或窥见他人草稿。草稿收集**用途**与**交付范围**
  （尺寸/格式/数量/是否含源文件），提交后进入报价流程。

## 六、管理页可追溯与下载包

- `audit.trace(project_id)` 汇总：审批链（谁、何时、批准哪版、内容哈希）、
  批准后变更差异、下载包记录、全部相关审计事件。
- `packages.build_package` 生成下载清单：每个作品取**最后一次获批的版本**
  （含版本号、文件哈希、批准单号、资产列表），附有效许可条款快照；
  授权已撤回的作品不入包。清单落库即留痕，交付依赖可回放。

## 运行

```bash
python3 -m unittest discover -s tests -v   # 27 个用例
python3 -m app.api                          # 启动 API（默认 127.0.0.1:8000）
```

主要端点：`POST /drafts` `PUT /drafts/{token}` `POST /drafts/{token}/submit`、
`POST /projects` `POST /projects/{id}/stages/{sid}/freeze|revisions`
`POST /projects/{id}/amendments|confirm|package`、
`POST /artworks/{id}/versions` `POST /projects/{id}/approvals`、
`POST /assets/{id}/permissions`、`POST /licenses` `POST /licenses/{id}/revoke`、
`POST /holds` `POST /holds/{id}/confirm`、`POST /derivative-jobs[/{id}/run]`、
`GET /search` `GET /share-card/{id}` `GET|POST /featured`、
`GET /projects/{id}/audit`。
