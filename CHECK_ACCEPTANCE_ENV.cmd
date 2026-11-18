@echo off
echo [TEST_ONLY] Tianxun Buddy V4.2.6 environment check
where python || echo BLOCKED: Python not found
where node || echo BLOCKED: Node.js not found
where npm || echo BLOCKED: npm not found
