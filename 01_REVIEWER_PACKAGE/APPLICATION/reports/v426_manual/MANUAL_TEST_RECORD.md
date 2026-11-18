# V4.2.6 前端人工验收记录（TEST_ONLY）

| 测试范围 | 状态 | 证据 |
|---|---|---|
| 启动器、后端健康、前端健康、隔离 | PASS | RUNTIME_LIFECYCLE.json |
| 免密码启动进入专用 Chrome OWNER | PASS | 专用 Chrome DOM 检查；登录表单消失，显示 owner · estimator |
| 12 个工作台入口与 1920×1080 | PASS | BROWSER_RESULTS.json |
| 12 个工作台入口与 1366×768 | PASS | BROWSER_RESULTS.json |
| 登录失败、会话保持、过期令牌、退出 | PASS | API_RESULTS.json、BROWSER_RESULTS.json |
| 项目创建、查询、切换、真实样本上传解析 | PASS | BROWSER_RESULTS.json |
| 土建 50 行、安装 98 行、合计 148 行导出 | PASS | API_RESULTS.json、BOQ_50.json、BOQ_98.json |
| OCR 页图、定位框、版本提交、审核退回、角色拒绝 | PASS | API_RESULTS.json、BROWSER_RESULTS.json |
| 材料价格期次、地域、勘误、来源追溯 | PASS | API_RESULTS.json |
| 算量、定额、换算、组价、复核、成果门禁 | PASS（TEST_ONLY） | BROWSER_RESULTS.json、API_RESULTS.json |
| 旧版浏览器回归 | PASS（7/7） | regression/FINAL_FRONTEND_RESULT.json |
| 后端全量回归 | PASS（1042/1042） | regression/FINAL_RESULT.json |
| 用户本人现场逐项手工验收 | NOT_TESTED | 需要用户实际操作 |
| 正式工程造价验收与专业签认 | BLOCKED | production_accepted=false；accuracy_E=BLOCKED |

测试账号密码仅保存在本机私有 `TEST_ACCOUNTS.private.md`，启动 OWNER 采用一次性本机令牌自动登录。未授予任何真实专业签认资格，测试操作不计入真实人工复核。
