# EXECUTION_STATE — V4.2.5 数据治理与人工复核

更新：2026-11-16T20:56:58.963136+08:00。队列：{'VERIFIED': 8, 'CHECKPOINTED': 1, 'BLOCKED': 3}。全量978通过，无失败无skip；浏览器3通过。生产验收False，E级BLOCKED。真实40份OCR仍待人工校对；2份DOC转换失败；没有切换业务数据库。

权威状态：scripts/V425_TASK_QUEUE.json；报告：V425_DELIVERY_REPORT.md；运行：docs/22_V425运行与验收.md；缺口：V425_UNRESOLVED.md。

---

# EXECUTION_STATE — V4.2.4 样本试点

更新：2026-11-16T17:18:35.345189+08:00。当前：{'VERIFIED': 12, 'CHECKPOINTED': 3, 'BLOCKED': 2}；全量回归869通过；浏览器2通过。E级精度BLOCKED，正式生产未验收。

权威状态：scripts/V424_TASK_QUEUE.json；报告：CORPUS_SAMPLE_PILOT_REPORT.md；缺口：V424_UNRESOLVED.md。

---

以下为原始交接与历史记录，完整保留。

# EXECUTION_STATE — V4.2.3 交接（持续开发机制已就位）

工程路径：`F:\ZHU-FISH\TIANXUN_BUDDY_V4\TianxunBuddy_V4_Execution_Starter`
版本：backend 0.4.2（目标 0.4.3）
基线提交：git `f75d238`（"V4.2.3 baseline: 105 passed + idem AttributeError"）

> 本文件的实时状态以 **`V423_EXECUTION_STATE.json`** 为准；本文件只记结构性结论与命令。
> 机制说明见 `docs/16_V423执行总控机制.md`。

## 0 自动进度（由 V423_SUPERVISOR 生成，勿手改标记区）

| 任务 | 状态 | 尝试 | 验证时间 |
| --- | --- | --- | --- |
<!-- V423:AUTO:START -->
| task                 | status       | att | verified_at |
| -------------------- | ------------ | --- | --- |
| P0-02-IDEM           | VERIFIED     | 1   | 2026-11-15 17:16:47 |
| P0-01-NUMERIC        | VERIFIED     | 1   | 2026-11-15 17:48:41 |
| P0-03-ADMISSION      | VERIFIED     | 1   | 2026-11-15 18:05:48 |
| P0-04-GATES          | VERIFIED     | 1   | 2026-11-15 18:27:27 |
| P0-05-JGATE          | VERIFIED     | 1   | 2026-11-15 18:53:45 |
| P0-06-EVIDENCE       | VERIFIED     | 1   | 2026-11-16 00:21:23 |
| P0-07-RECALC         | VERIFIED     | 1   | 2026-11-15 19:43:08 |
| P1-01-JOBMODEL       | VERIFIED     | 1   | 2026-11-15 19:08:53 |
| P1-02-SQLITE-REPO    | VERIFIED     | 1   | 2026-11-15 19:27:56 |
| P1-03-PG-REPO        | VERIFIED     | 3   | 2026-11-15 22:14:11 |
| P1-04-MIGRATION      | VERIFIED     | 1   | 2026-11-15 23:15:40 |
| P1-05-TX-RECOVERY    | VERIFIED     | 1   | 2026-11-15 23:36:36 |
| P1-06-FILESTORE      | VERIFIED     | 1   | 2026-11-16 00:10:18 |
| P1-07-ACL            | VERIFIED     | 1   | 2026-11-16 02:17:41 |
| P1-08-YAML-WF        | VERIFIED     | 1   | 2026-11-16 00:35:08 |
| P1-09-PARSE          | VERIFIED     | 2   | 2026-11-16 00:58:32 |
| P1-10-DELIVER        | VERIFIED     | 1   | 2026-11-16 01:26:16 |
| P1-11-AUTH           | VERIFIED     | 1   | 2026-11-16 01:53:46 |
| P1-12-FRONTEND       | VERIFIED     | 1   | 2026-11-16 02:32:08 |
| A-00-ACCEPT          | VERIFIED     | 1   | 2026-11-16 03:05:04 |
| A-01-PACKAGE         | VERIFIED     | 1   | 2026-11-16 03:10:28 |

progress: 21/21 VERIFIED   supervisor pid=3280   heartbeat=2026-11-16T03:10:28.3819972+08:00
generated: 2026-11-16 03:10:28  (do not edit inside the markers)
<!-- V423:AUTO:END -->

## 1 第 3 轮（2026-11-15 17:00 起）已完成

| 项 | 结论 | 证据 |
| --- | --- | --- |
| 保存既有修改 | 建立本地 git 基线 `f75d238`，工作树干净 | `git log --oneline` |
| 恢复 Python 环境 | `backend/.venv` 重建，fastapi 0.163 / pytest 8.4.2 / pydantic 2.14.3 | `backend/scripts/run.ps1` 约定 |
| **幂等缺陷根因** | `AttributeError: 'EvidenceStore' object has no attribute 'idem_claim'` —— api.py 重写后调用 `idem_claim`/`idem_finish`，但 store.py 从未实现 | `logs/idem_full_trace.log`、`logs/idem_exit_code.txt` |
| **幂等修复** | store.py 新增数据库级原子认领：`idem` 表加 `state/claimed_at`+可空 `response`；认领裁判 = `INSERT..ON CONFLICT DO NOTHING` 后 `changes()==1`；`idem_finish` 置 DONE；异常 `idem_release`；300s 陈旧认领 CAS 接管 | `backend/txbuddy/store.py`、`backend/txbuddy/api.py` |
| **并发取证** | 双场景各 10 轮 × 50 并发：同键同参真实执行恒为 1 次；同键异参 1×200 + 49×409 | `logs/idem_evidence_20261115T084801Z.md` |
| 全量回归 | **106 passed / 2 skipped / 0 failed** | `logs/regression_after_idem.log` |
| **持久化总控** | `V423_SUPERVISOR.ps1` + `V423_TASK_QUEUE.json`（21 任务）+ `V423_WORKER.ps1` 已实跑验证 | `V423_EXECUTION_LOG.md` |
| 第一任务闭环 | P0-02-IDEM：Worker 起新会话 → 产出 5 个语义用例 → 总控独立跑 3 条验收命令全绿 → VERIFIED | `tasks/P0-02-IDEM/`、`logs/verify/P0-02-IDEM_*.log` |

## 2 当前运行中的执行机制

```
scripts/V423_SUPERVISOR.ps1  (常驻, -Detach)
   └── scripts/V423_WORKER.ps1  (每任务一个)
          └── dsh --profile headless --permission-mode danger-full-access   (每任务一个全新会话)
```

- 状态机：PENDING → RUNNING → CHECKPOINTED/FAILED → VERIFIED；BLOCKED 必须带诊断任务 D-<ID>
- **COMPLETED ≠ VERIFIED**：只有总控重跑 `tasks/<ID>/TEST.md` 全部 exit 0 才升级
- 失败 3 次 → 生成独立诊断 Worker，禁止继续重复同一修复路径
- 上下文耗尽/进程异常 → 读 `tasks/<ID>/CHECKPOINT.json` 续跑
- 停止：`New-Item -ItemType File -Force logs/STOP`

## 3 任务队列（21 项，实时状态见 V423_EXECUTION_STATE.json）

P0：P0-02-IDEM ✅VERIFIED｜P0-01-NUMERIC｜P0-03-ADMISSION｜P0-04-GATES｜P0-05-JGATE｜P0-06-EVIDENCE｜P0-07-RECALC
P1：P1-01-JOBMODEL｜P1-02-SQLITE-REPO｜P1-03-PG-REPO｜P1-04-MIGRATION｜P1-05-TX-RECOVERY｜P1-06-FILESTORE｜P1-07-ACL｜P1-08-YAML-WF｜P1-09-PARSE｜P1-10-DELIVER｜P1-11-AUTH｜P1-12-FRONTEND
验收：A-00-ACCEPT（十类统一验收）｜A-01-PACKAGE（验收未 VERIFIED 时拒绝打包）

## 4 未开始 / 可能 BLOCKED（不得标记完成）

- PostgreSQL 实测：本机未确认有 psql/docker/WSL 实例；若探测不到，P1-03 只能 BLOCKED 并附证据
- L1 YAML 工作流执行器（当前 L1 仍是 API 手工编排）
- 真实文件上传与 PDF/DOCX/DXF/DWG 解析
- 成果文件生成（预算书 XLSX/PDF、工程量计算书等 10 类）
- 身份认证与权限体系（当前只有 X-Actor 标记）
- 前端业务工作台（当前仅诊断页签）
- 端到端合成项目测试

## 5 常用命令

统一入口一律走项目根目录的 CMD（内部固定 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File`）：

```bat
cd /d F:\ZHU-FISH\TIANXUN_BUDDY_V4\TianxunBuddy_V4_Execution_Starter
RUN_V423_STATUS.cmd                          :: 看状态（只读，绝不启动总控）
RUN_V423_RESUME.cmd                          :: 恢复/常驻推进（已有总控在跑时只打印状态）
RUN_V423_WORKER.cmd -TaskId P0-06-EVIDENCE   :: 手工单任务调试
RUN_V423_ACCEPTANCE.cmd                      :: 十类验收
```

其余运维动作仍需显式带策略开关，否则新进程会被 Restricted 拦下：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\package.ps1        # 打包闸门
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\deploy.ps1         # 重建环境
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\restore.ps1 -Backup # 快照
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\V423_ENTRY_AUDIT.ps1 # 启动层回归审计
```

> 执行策略根因与实测证据见 `docs/16_V423执行总控机制.md` §9。

## 6 纪律（来自 V4.2.3 要求，未变）

不得删除失败测试、不得降低断言、不得用 skip 冒充通过、不得伪造测试通过；
验收未全部通过前不得打包；后续扩展模块不得标记为已完成。

## 7 交付结论（2026-11-16 03:10）——**首次交付，历史**

> 本节记录的是第一次打包时的状态。该包含 `backend\.pg-dsn`，已判为不安全并归档为 SUPERSEDED。
> **当前状态以 §8 为准。**

| 项 | 结果 | 证据 |
| --- | --- | --- |
| 任务队列 | **21/21 VERIFIED，0 BLOCKED** | `V423_EXECUTION_STATE.json`、`V423_EXECUTION_LOG.md`、`logs/supervisor.log` |
| 十类验收 A-00 | **11/11 PASS**（Worker 自测 + 总控独立重跑各一次） | `reports/ACCEPTANCE_20261116T025301.md`、`...T025952.md` |
| 打包 A-01 | **VERIFIED**，zip + SHA256SUMS | `release/TianxunBuddy_V4.2.3_Engineering_Hardening.zip`（454 条目，0.71MB） |
| 包哈希 | `237958026bfcc450250e034c5562176ce72afef077b641fc9b7f005d7a7a4178`（declared == recomputed） | `release/SHA256SUMS` |
| PostgreSQL | 真跑：本机 16.4 实例 127.0.0.1:55432/txbuddy，`test_v423_pg_repository.py` **43 passed / 0 skipped** | `tasks/P1-03-PG-REPO/PG_PROBE.md`、`logs/verify/P1-03-PG-REPO_1_20261115T221337.log` |
| 独立验收原则 | COMPLETED ≠ VERIFIED 全程成立；P1-03、P1-09 各因超时/失败重试后才 VERIFIED | `logs/supervisor.log` |
| 总控状态 | 队列排空后自行退出；supervisor/worker 进程数均为 0 | 任务结束日志 `ALL TASKS VERIFIED` |

**已知遗留（不隐藏）**：

1. 包内包含 `backend\.pg-dsn`（本机开发 DSN，trust 认证、无口令）；换机器需改该文件或设 `TXBUDDY_PG_DSN`。
2. 本机 PostgreSQL 是用户空间集群（`F:\ZHU-FISH\PG_LOCAL`），trust 认证、无备份、无高可用，**仅开发期使用**。
3. 全量回归仍有 2 个 skip（`test_pg_integration.py` 依赖 `TX_PG_DSN`，属既有设计），未当作通过。
4. 成果文件与真实定额/费率/信息价仍属 TEST_ONLY；业务结论不因自动化通过而改变。

## 8 安全修正与交付审计（2026-11-16 10:10–10:20）——**当前状态**

| 项 | 值 |
| --- | --- |
| 任务队列 | 21/21 VERIFIED（未重置、未重跑） |
| 全量测试 | **824 passed / 2 skipped**（收集 826） |
| 十类验收（复验） | **11/11 PASS，exit 0** → `reports/ACCEPTANCE_20261116T101547.md` |
| 浏览器渲染冒烟 | PASS（Chrome 157 headless，DOM 含「后端已连接」）→ `logs/e2e/shot_20261116T100846.png` |
| 交付包（当前） | `release/TianxunBuddy_V4.2.3_Safe_20261116.zip`，sha256 与文件数以 `release/SHA256SUMS` + `release/PACKAGE_REPORT.md` 为准 |
| 敏感扫描 | staged 340 + archive 340 文本条目，**hard=0**，soft=3（占位符/夹具） |
| 解包校验 | 457 文件 / 0 不一致 / 0 敏感文件 |
| 五方一致 | zip = SHA256SUMS = state.package = PACKAGE_REPORT = A-01 RESULT 规范块 = 本次 verify 日志 |

### 8.1 本轮修掉的问题

1. **P0 泄露**：`package.ps1` 只用 `/XD`（目录）未用 `/XF`（文件），`backend\.pg-dsn` 进了包。已加 `/XF` 清单 + 打包前扫描 + 打包后扫描 + 两级负向测试。
2. **P1 审计漂移**：`RESULT.md` 手写、产物由脚本生成，总控复核再打包即漂移（`0be6b72e…` vs 实际）。已改为脚本同时产出 `PACKAGE_REPORT.md|json` 与 RESULT 标记块。
3. **入口缺陷**：`acceptance.ps1` 不加载 `backend/.pg-dsn` → 普通 shell 跑验收时 PG 用例 41 个 setup error（9/11 假失败）。已收敛到 `Initialize-V423PgEnv`，总控与验收共用。
4. **死参数**：`package.ps1 -ReleaseName` 被硬编码 `$ZipName` 覆盖，安全版无法另名。已修。
5. **版本标识**：`api.py` 的 `app.version` / `/health.version` 由 `0.4.2` 统一为 `0.4.3`。

### 8.2 交付物清单（当前）

| 路径 | 说明 |
| --- | --- |
| `release/TianxunBuddy_V4.2.3_Safe_20261116.zip` | 安全修正版交付包 |
| `release/SHA256SUMS` | 交付包哈希（只覆盖 zip） |
| `release/PACKAGE_REPORT.md` / `.json` | 每次打包自动生成的权威审计记录 |
| `release/history/20261116T0310_pre_fix/` | 旧包 + SUPERSEDED 说明（不得分发） |
| `handoff/V423_HANDOFF_20261116.zip` | 交接包（**建于修复前，内部含旧包，需重打**） |
| `docs/18_集成差距与安全审计.md` | 认证 6 问实测、7 个未接通端点、打包与审计修复 |
| `docs/17_交接包_外部智能体.md` | 自包含交接包 |

### 8.3 仍然存在的差距（不得读成已完成）

1. **认证与 ACL 未挂主应用**：无认证中间件，任意调用方可读写任意项目（实测 200）；内部口令默认值 `dev-internal` 应改 fail-closed。
2. **7 个端点未接通**：`/v1/jobs`、`/files/{path}`、`/v1/parse`、`/v1/deliverables/{job_id}`、`/v1/deliverables/{job_id}/generate`、`/auth/login`、`/projects/{project_id}`（详见 docs/18 §5）。
3. **定额库/价格库只有 DDL**（TX-012 / TX-015 仍 NOT_STARTED）。
4. 真实定额/费率/信息价与 18 套样本缺失，成果仍 TEST_ONLY。
5. 交互式浏览器 E2E 未做（本机无 playwright/puppeteer），仅有渲染冒烟。


## V4.2.4 Codex 接管状态

分支 `codex/v4.2.4-sample-pilot`。完整快照和基线证据见仓库外 `_CODEX_V424/takeover_20261116T153940`。本轮任务权威状态为 `scripts/V424_TASK_QUEUE.json`，执行记录为 `V424_EXECUTION_LOG.md`。样本验证与正式计算分别验收；精度 E 为 BLOCKED。

---

## V4.2.6 执行状态（独立区块，2026-11-16）

权威状态文件：`V426_EXECUTION_STATE.json`；任务队列：`scripts/V426_TASK_QUEUE.json`。V4.2.5 及更早历史区块保持原样。

- 安全修复、后端 1036 项回归、前端构建及 4 项浏览器回归：`VERIFIED`。
- 真实材料样本和 B_REFERENCE 清单：`CHECKPOINTED`，分别覆盖 10989 条价格和 148 行清单。
- OCR 真实人工复核、专业签认、正式当前价格、跨地域、独立算量、正式工程造价闭环、生产 PostgreSQL 迁移：`BLOCKED`。
- 生产接受：`false`；精度 E：`BLOCKED`。
- 最终回归：后端 1036 passed / 0 failed / 0 skipped，前端构建通过且浏览器 4 passed，打包回归 21 passed；当前源码与前后端测试 manifest 一致。
- 数据完整性：251 个原始文件哈希未变，受检 4 个 SQLite 数据库逻辑内容未变，integrity_check=ok，外键异常 0。
- 安全包：`release/TianxunBuddy_V4.2.5.1_SecurityFix_*.zip` 与 `release/TianxunBuddy_V4.2.6_BusinessValidation_*.zip`；最终文件及独立解包校验以 `release/v426/LATEST_DELIVERY.json` 为准，历史包保留。
- 文件大小与 SHA256 索引：`reports/v426/DELIVERY_FILE_INDEX.json`，包内全部载荷哈希另见 `SHA256SUMS`。
