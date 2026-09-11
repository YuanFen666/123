@echo off
setlocal enabledelayedexpansion
title chaoxing-fixed 一键启动
cd /d "%~dp0"

rem ================================================================
rem  关于代码页（很重要，别随手加 chcp）
rem  ------------------------------------------------------------------
rem  本脚本是 GBK(936) 编码的，而中文 Windows 的 cmd 默认代码页就是 936，
rem  所以不需要 chcp 也能正常显示中文。
rem  反过来，一旦调用 chcp（哪怕只是 chcp 查一下），cmd 会重建控制台，
rem  把「重定向进来的 stdin」丢掉 —— 之后所有 set /p 都读到空值，
rem  菜单会陷入死循环。实测：chcp 936 / chcp 936 >nul / 子 cmd 里 chcp
rem  全都会触发这个问题。
rem  所以这里改成：先读注册表拿到系统 OEM 代码页，只有确实不是 936 时才切。
rem ================================================================
set "OEMCP="
for /f "tokens=3" %%i in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Nls\CodePage" /v OEMCP 2^>nul') do set "OEMCP=%%i"
if defined OEMCP if not "!OEMCP!"=="936" chcp 936 >nul

set "EXE=%~dp0chaoxing-3.1.4-fixed.exe"
set "CFG=%~dp0config.ini"

if not exist "%EXE%" (
    echo.
    echo  [错误] 找不到 chaoxing-3.1.4-fixed.exe，请确认它和本脚本在同一个目录。
    echo.
    pause
    exit /b 1
)
if not exist "%CFG%" (
    echo.
    echo  [错误] 找不到 config.ini。
    echo         请先把 config.ini.example 复制成 config.ini，并填好账号密码与题库/API Key。
    echo.
    pause
    exit /b 1
)

:MENU
cls
echo.
echo  ================================================================
echo    chaoxing 修复版   一键启动
echo  ================================================================
echo     [1] 视频 + 章节答题模式
echo         刷课：视频 / 文档 / 阅读 / 章节测验（自动查题作答）
echo.
echo     [2] 考试模式
echo         只做考试看板 + 就绪体检（只读，不刷课、不替你进考场）
echo.
echo     [0] 退出
echo  ================================================================
echo.
set "MODE="
set /p "MODE=请输入模式编号后回车: "

if "%MODE%"=="1" goto PICK_VIDEO
if "%MODE%"=="2" goto PICK_EXAM
if "%MODE%"=="0" exit /b 0
echo.
echo  输入无效，请重新选择。
pause >nul
goto MENU


:PICK_VIDEO
call :PICK_COURSE
echo.
echo  ----------------------------------------------------------------
echo   ^>^>^> 视频 + 章节答题模式启动...
echo  ----------------------------------------------------------------
echo.
"%EXE%" -c "%CFG%" %COURSE_ARG%
goto DONE


:PICK_EXAM
call :PICK_COURSE
echo.
echo  ----------------------------------------------------------------
echo   ^>^>^> 考试模式启动（只读：列出考试、状态、截止时间、能否开考）
echo  ----------------------------------------------------------------
echo.
"%EXE%" -c "%CFG%" --exam-only %COURSE_ARG%
goto DONE


:DONE
echo.
echo  ================================================================
echo   程序已退出。按任意键关闭本窗口。
echo  ================================================================
pause >nul
exit /b 0


rem ================================================================
rem  选择课程
rem   调用 exe 的 --list-courses，它以「序号^|课程名^|courseId」的纯文本打到
rem   stdout（程序日志走 stderr，不会混进来），这里解析成菜单。
rem   返回：COURSE_ARG = -l "id1,id2"，或 --all-courses（全部课程）
rem ================================================================
:PICK_COURSE
set "COURSE_ARG=--all-courses"
cls
echo.
echo   正在读取课程列表（需要登录，约几秒）...
echo.
set "N=0"
for /f "usebackq tokens=1,2,3 delims=|" %%a in (`""%EXE%" -c "%CFG%" --list-courses"`) do (
    set /a N+=1
    set "C%%a=%%c"
    echo     [%%a] %%b
)
echo.
if "!N!"=="0" (
    echo   [!] 没读到课程列表（可能是登录失败或网络问题），已中止。
    echo       请先确认 config.ini 里的账号密码是否正确，或直接运行
    echo       chaoxing-3.1.4-fixed.exe -c config.ini 看具体报错。
    echo.
    pause
    exit /b 0
)
echo     [0] 全部课程（共 !N! 门）
echo.
set "PICK="
set /p "PICK=请输入课程编号（多个用英文逗号分隔，直接回车=全部课程）: "
if not defined PICK goto :eof
if "%PICK%"=="0" goto :eof

set "IDS="
set "BAD="
for %%p in (%PICK%) do (
    call set "V=%%C%%p%%"
    if defined V (
        set "IDS=!IDS!,!V!"
    ) else (
        set "BAD=!BAD! %%p"
    )
)
if defined BAD echo.
if defined BAD echo   [!] 编号!BAD! 不存在，已忽略。
if not defined IDS (
    echo   [!] 没有选到有效课程，改为全部课程。
    goto :eof
)
set "COURSE_ARG=-l "!IDS:~1!""
echo.
echo   已选择课程ID：!IDS:~1!
goto :eof
