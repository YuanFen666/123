@echo off
chcp 936 >nul
title chaoxing-fixed 一键启动
cd /d "%~dp0"
echo.
echo  ============================================
echo   chaoxing 修复版  一键启动
echo   配置: config.ini（与本文件同目录）
echo  ============================================
echo.
"%~dp0chaoxing-3.1.4-fixed.exe" -c "%~dp0config.ini"
echo.
echo  程序已退出。按任意键关闭本窗口。
pause >nul
