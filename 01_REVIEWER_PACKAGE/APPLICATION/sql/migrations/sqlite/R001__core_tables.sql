-- R001: 回滚 V001（SQLite）
-- 顺序与依赖相反：先删审计日志，再删计算运行，最后删证据。
-- IF EXISTS 保证"回滚本身也可重复执行"（已回滚的库再回滚一次不报错）。

DROP TABLE IF EXISTS audit_log;
DROP TABLE IF EXISTS calc_run;
DROP TABLE IF EXISTS evidence;
