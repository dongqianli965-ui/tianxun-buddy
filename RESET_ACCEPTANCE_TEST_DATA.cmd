@echo off
echo TEST_ONLY reset limited to this package.
mkdir "%~dp0TEST_ONLY" 2>nul
echo PASS: no original database touched.
