# 天寻 Buddy V4.2.6 前端人工测试

本轮交付基于 V4.2.6 BusinessValidation。`production_accepted=false`，`accuracy_E=BLOCKED`。本机环境仅用于 TEST_ONLY 操作与技术验收；清单中的工程量来自原表，不是独立算量成果。自动测试中的复核动作不计真实人工复核。

## 一键使用

1. 在项目根目录双击 `RUN_V426_FRONTEND_MANUAL.cmd`。脚本检测 Python、Node、npm、依赖、Chrome、端口与本机私有源配置，核对原件和源数据库，再建立独立 PostgreSQL 克隆和 SQLite。首次启动复制样本、还原数据库需要一段时间；不要重复点击。
2. 等待 RUNNING，Chrome 会打开 <http://127.0.0.1:5173>。后端是 <http://127.0.0.1:8010>。页头应显示 V4.2.6、TEST_ONLY、后端已连接与当前身份。
3. Chrome 会自动以 TEST_ONLY OWNER 身份进入工作台，无需输入密码。要测试其他身份或登录、退出和权限时，可查看控制台打印的 `TEST_ACCOUNTS.private.md`，使用其中的临时测试账号。密码不会放入本交付包，每个新会话重新生成。不要把该文件提交或分享。
4. 双击 `CHECK_V426_FRONTEND_MANUAL.cmd`，确认服务 PID 身份、/health、PostgreSQL 隔离及原件完整性。端口占用时脚本标记 BLOCKED，不结束其他软件。
5. 测试结束双击 `STOP_V426_FRONTEND_MANUAL.cmd`。仅停止本次记录且 PID、创建时间、命令行均匹配的服务、观察进程及专用 Chrome 进程；保留账号、测试库、日志与成果供复查。关闭前后再次核对原件。其他 Chrome 窗口不属于停止范围。

本机依赖默认来自相邻 `_CODEX_V424/venv`、`_CODEX_V425/vendor`、`PG_LOCAL/pgsql/bin`。启动器会优先检测本机已准备的环境；缺少依赖或不能证明隔离时保留 BLOCKED，不尝试使用生产连接代替测试库。源码包本身不含依赖安装目录、数据库与原始资料。迁移到其他电脑时需按 backend/requirements.txt、frontend/package-lock.json 准备依赖，并准备只读样本源和可创建克隆库的本机私有配置。

## 隔离目录与日志

会话目录在项目上一级 `_CODEX_V426/manual/TEST_ONLY-时间-随机值`，不在源码目录中。`latest.private.json` 指向最近会话。

- `TEST_ACCOUNTS.private.md/json`：五类临时账号及样本副本路径。没有专业签认资格。
- `runtime/`：业务 SQLite、OCR 复核库、ACL、上传文件、派生成果。
- `source-copies/`、`ocr-assets/`：真实文件及 OCR 页图副本；前端访问副本，原始资料不进入写入链路。
- `config.private.json`：随机 JWT、内部 Token、隔离数据库 DSN。不要提交或打包。
- `logs/`：后端、前端、接口请求响应、浏览器网络与错误记录。口令、Token、二进制原件不写入接口证据正文。
- `browser-captures/`：专用 Chrome 页面变化截图（约每 5 秒采样）；截图与完整业务日志只留本机私有目录。不要在该专用窗口打开其他网站。页面输入和业务数据可能出现在截图中。
- `ORIGINAL_BEFORE.private.json`、`ORIGINAL_CHECK.private.json`、`ORIGINAL_AFTER_STOP.private.json`：原件 SHA256、SQLite 逻辑数据/完整性、PostgreSQL 只读逻辑快照。

RUN/CHECK 的任一步返回 BLOCKED 时，查看本机日志中的具体原因。不要编辑 DSN 使程序直接连接源库，也不要为了启动而关闭隔离断言。源库权限或外部资料不具备时继续保留 BLOCKED。

## 建议人工操作顺序

每项记录 `PASS / FAIL / BLOCKED / NOT_TESTED`，附截图、接口状态及预期/实际。`reports/v426_manual` 是本轮技术测试证据；尚未由用户亲自执行的人工验收不得写成 PASS。

1. **身份和项目**：OWNER 正常登录、错误密码、刷新保持、退出；创建两个 TEST_ONLY 项目，查询并切换。UNAUTHORIZED 载入 OWNER 的 Job ID 应拒绝。观察当前身份与项目角色是否随切换更新。过期 Token 由自动化安全测试覆盖；也可等待一小时 TTL 后重试。
2. **资料与清单**：样本试点选私有说明中的副本，上传 PDF/DOCX/XLSX，核对显示的 SHA256，点击解析查看结构。对 B_REFERENCE 的土建/安装清单分别导入，核对 50 / 98 行及编码、名称、特征、单位、数量、来源行号。清单页还可单独导入 XLSX、写入项目记录和带入定额。样本试点执行样本流程后生成、下载 XLSX，并与原表逐行核对。不得把原表数量称为独立算量。
3. **OCR**：EDITOR 搜索 173，打开原始页图、切页、查看文字定位框与置信度，修改文本及 JSON 字段并保存草稿。提交已保存版本；未保存变更应拒绝提交。REVIEWER 登录后针对准确提交版本批准或退回。EDITOR 审批和 VIEWER 修改必须拒绝。无真实专业身份签认必须拒绝。核对历史链、版本及 SHA256。自动化预先生成的 TEST_ONLY 事件不要算作真实人工复核。
4. **材料价格**：样本试点分别查 2026-Q2、Q3、09，核对勘误及来源页码；同名同规格保留多条候选。错误地域、缺失期次必须明确拒绝。候选查询不构成正式价格确认，也不覆盖原价。
5. **算量、定额、组价**：依次打开规则算量、固定算子、定额匹配、附注换算、综合单价、指数调差、项目总价。填写明确标注 TEST_ONLY 的算术数据，核对返回值与依据；检查空值、非法 JSON、非法数值的提示。正式计算开关在当前未核验依据下应被服务端拒绝。真实工程精度仍 BLOCKED。
6. **复核、成果、证据**：执行门禁、用最近 calc_run 填充复算、读取任务并尝试审批；L6 项目才进行鉴定影子运行。刷新成果前置条件，正式成果应拒绝；样本成果从样本试点下载。证据页读取证据、重算摘要验签、读取审计及 calc_run。
7. **视觉交互**：在 1920×1080 和 1366×768 下逐页检查中文、横向溢出、表格滚动、加载提示、上传反馈、错误提示及数据刷新。切换页面将重新载入该页表单；当前项目和登录态保持。未保存的临时输入请先保存或自行记录再切换。

## 执行自动复验

已有后端、原 4 项 Chrome 浏览器与原 21 项打包专项测试均保留。本轮新增人工环境测试、逐页浏览器测试和 API 场景，不降低旧断言。全量报告及失败重跑保存在 `reports/v426_manual`。

自动化浏览器测试使用本机已启动的隔离环境，设置 `TXBUDDY_MANUAL_SESSION` 为会话路径后，在 frontend 运行 `npm run test:e2e -- --config playwright.manual.config.ts`。此命令将操作测试副本并保存截图；会生成清晰标注的 TEST_ONLY OCR 工作流记录。不要指向其他环境。

独立后端回归使用 `scripts/v426_manual_regression.py`，旧浏览器回归使用 `scripts/v426_manual_legacy_browser.py`。它们使用新的隔离源码副本与数据库，不覆盖 `reports/v426` 的历史交付证据。

任何资料授权、专业资格、独立计算真值或真人复核条件不足的正式业务继续标记 BLOCKED；本轮前端环境可用不代表正式工程造价业务验收通过。
