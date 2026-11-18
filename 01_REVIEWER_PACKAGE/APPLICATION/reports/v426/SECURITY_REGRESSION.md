# V4.2.6 安全回归索引

- 后端修复前：`reports/v426/acl_pre_fix.log`（20 failed / 17 passed）。
- 后端最终：`reports/v426/FINAL_BACKEND.log`、`FINAL_BACKEND.xml`、`FINAL_RESULT.json`（1036 passed / 0 failed / 0 skipped）。
- 前端最终：`FINAL_FRONTEND_BUILD.log`、`FINAL_FRONTEND_BROWSER.log`、`FINAL_FRONTEND_RESULT.json`（build 0，Playwright 4 passed）。
- 数据完整性：`FINAL_DATA_INVENTORY.json`（251 原始文件 unchanged、live DB logically unchanged、SQLite integrity/FK passed）。
- OCR ACL：`ACL_REGRESSION.log`、`OCR_ROLE_BROWSER_EVIDENCE.json`。
- OCR Store：`OCR_STORE_FINAL.md`、`OCR_CONTENT_DEDUP.json`、`ocr_store_before.log`（8 项修复前失败）。
- 业务：`BUSINESS_PRE_FIX.log`、`BUSINESS_REGRESSION.log`、`MATERIAL_PRICE_VALIDATION.json`、`BOQ_VALIDATION.json`。
- 打包：`scripts/package_v426.py`、`release/v426/` 下两个 profile 的报告与 SHA256。

## 最终复核

基线后端在捕获副本实测 978 passed / 0 skipped（排除本轮 V426 新增测试），最终后端 1036 passed / 0 failed / 0 errors / 0 skipped，另有 1 条 Starlette/httpx 弃用警告。旧尝试及修复前失败日志保留，最终结论只使用无 attempt 后缀的 FINAL 证据。

最终前端 build exit 0、Playwright 4 passed / 0 unexpected / 0 skipped / 0 flaky，使用与最终后端相同的隔离副本。前后端 tested_source_sha256 均与当前源码逐文件一致，测试期间源码未变。打包专项 21 passed / 0 failed / 0 errors / 0 skipped，见 `PACKAGE_TESTS.log` 与 `PACKAGE_TESTS.xml`。

数据完整性复核覆盖 251 个原始文件和 4 个 SQLite 数据库，文件哈希、数据库逻辑内容不变，integrity_check=ok、外键异常 0。包外独立解压、CRC、SHA256SUMS 覆盖、逐文件哈希、敏感值扫描及测试源码一致性的最终结果由 `release/v426/LATEST_DELIVERY.json` 指向，不将历史包当成最终包。

所有自动化通过均不等于工程造价业务验收；`production_accepted=false`，`accuracy_E=BLOCKED`。
