@echo off
setlocal enabledelayedexpansion
title chaoxing-fixed 一键启动
cd /d "%~dp0"

rem ================================================================
rem  关于代码页（很重要，别随手加 chcp）
rem  ------------------------------------------------------------------
rem  本脚本是 GBK(936) 编码的，中文 Windows 的 cmd 默认代码页就是 936，
rem  不需要 chcp 也能正常显示中文。
rem  反过来，只要调用 chcp（哪怕只是 chcp 查一下），cmd 会重建控制台，
rem  把「重定向进来的 stdin」丢掉 —— 之后所有 set /p 都读空值、菜单死循环。
rem  实测：chcp 936 / chcp 936 >nul / 子 cmd 里 chcp 全都会触发。
rem  所以这里先读注册表拿系统 OEM 代码页，只有确实不是 936 时才切。
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

rem ================================================================
rem  选账号：只在这里问一次，然后把 --account-index 传给后面每一次运行。
rem  必须这样做 —— 下面 :PICK_COURSE 里的 --list-courses 是用 for /f 抓输出的，
rem  那个子进程的 stdout 被吞掉，如果让它自己去问账号，菜单根本显示不出来，
rem  程序会静默卡死在等一个你看不见的输入上（实际踩过这个坑）。
rem ================================================================
set "ACCOUNT_ARG="
set "NACC=0"
for /f "usebackq tokens=1,2 delims=|" %%a in (`""%EXE%" -c "%CFG%" --list-accounts"`) do (
    set /a NACC+=1
    set "ACC%%a=%%b"
)
rem 第一步：选登录账号（先登录，再选模式）
set "ACCSHOW=!ACC1!"
if !NACC! GTR 1 (
    cls
    echo.
    echo  ================================================================
    echo    第一步：请选择要登录的账号
    echo  ================================================================
    echo    本次运行（刷课或考试）都会使用你选中的这个账号：
    echo.
    for /l %%i in (1,1,!NACC!) do echo     [%%i] !ACC%%i!
    echo  ================================================================
    echo.
    set "ACCPICK="
    set /p "ACCPICK=请输入账号编号后回车（直接回车=第 1 个）: "
    if defined ACCPICK (
        call set "V=%%ACC!ACCPICK!%%"
        if defined V (
            set "ACCOUNT_ARG=--account-index !ACCPICK!"
            set "ACCSHOW=!V!"
        ) else (
            echo.
            echo   编号无效，使用第 1 个账号。
            ping -n 2 127.0.0.1 >nul
        )
    )
)
if !NACC! EQU 1 echo    当前账号：!ACCSHOW!  ^(config.ini 里只有一组^)


:MENU
cls
echo.
echo  ================================================================
echo    chaoxing 修复版   一键启动
echo  ================================================================
echo    当前账号：!ACCSHOW!
echo  ----------------------------------------------------------------
echo    第二步：请选择要做什么
echo     [1] 视频 + 章节答题模式（只刷课，不考试）
echo         刷课：视频 / 文档 / 阅读 / 章节测验（自动查题作答）
echo.
echo     [2] 考试模式
echo         考试看板 / 就绪体检 / 进入考场自动答题
echo         ※ 进考场答题会自动读取所需入口参数，无需手工配置
echo           （没填也能用第 1 档「只看考试情况」，那一档是只读的）
echo           进去后菜单里还有完整【使用说明】
echo.
echo     [3] 视频 + 章节答题 + 考试   一并完成
echo         先刷课，全部跑完后接着把选中课程的考试也做掉
echo.
echo     [4] 视频 + 章节答题 + 考试   一并完成 + 自动交卷
echo         同上，但答完直接提交，不再等你核对
echo         ※ 每场只有一次提交机会，进考场即开始计时，确认再选
echo.
echo     [0] 退出
echo  ----------------------------------------------------------------
echo    ^* 用得顺手的话，来给项目点个 Star 吧（不点也完全不影响使用）:
echo      github.com/YuanFen666/chaoxing-fixed
echo  ================================================================
echo.
set "MODE="
set /p "MODE=请输入模式编号后回车: "

if "%MODE%"=="1" goto PICK_VIDEO
if "%MODE%"=="3" (
    set "EXTRA_ARG=--exam-take"
    goto PICK_VIDEO
)
if "%MODE%"=="4" (
    set "EXTRA_ARG=--exam-take --exam-submit"
    goto PICK_VIDEO
)
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
"%EXE%" -c "%CFG%" %ACCOUNT_ARG% %EXTRA_ARG% %COURSE_ARG%
goto DONE


:PICK_EXAM
cls
echo.
echo  ================================================================
echo    考试模式
echo  ================================================================
echo    【使用说明】
echo     - 第 1 档只读，不进考场；第 2/3 档会真的进考场替代你作答。
echo     - 进考场就开始计时、通常只有一次机会：第一次建议用第 2 档，
echo       让程序答题但由你自己核对后交卷，确认靠谱了再用第 3 档。
echo     - 每次只处理一场考试，答完就停，不会自动接着考下一门。
echo     - 命中本地 cache.json / answer_key.json 的题目不消耗任何题库额度。
echo  ----------------------------------------------------------------
echo     [1] 只看考试情况（只读）
echo         列出考试、状态、截止时间、能否开考、要不要人脸/验证码
echo.
echo     [2] 进考场自动答题，但不交卷   ^<== 第一次建议用这个
echo         程序替你答题并逐题保存，最后你自己核对后点交卷
echo.
echo     [3] 进考场自动答题并自动交卷
echo         真正把这次考试做完并交上去（覆盖率达标才交）
echo.
echo     [0] 返回上一级
echo  ================================================================
echo.
set "EXAMOPT="
set /p "EXAMOPT=请输入编号后回车: "
if "%EXAMOPT%"=="0" goto MENU
if "%EXAMOPT%"=="1" (
    set "EXAM_ARG=--exam-only"
    goto EXAM_RUN
)
if "%EXAMOPT%"=="2" (
    set "EXAM_ARG=--exam-only --exam-take"
    goto EXAM_RUN
)
if "%EXAMOPT%"=="3" (
    set "EXAM_ARG=--exam-only --exam-take --exam-submit"
    goto EXAM_RUN
)
echo.
echo  输入无效，请重新选择。
pause >nul
goto PICK_EXAM

:EXAM_RUN
call :PICK_COURSE
echo.
echo  ----------------------------------------------------------------
echo   ^>^>^> 考试模式启动（%EXAM_ARG%）
echo  ----------------------------------------------------------------
echo.
"%EXE%" -c "%CFG%" %ACCOUNT_ARG% %EXAM_ARG% %COURSE_ARG%
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
for /f "usebackq tokens=1,2,3,4 delims=|" %%a in (`""%EXE%" -c "%CFG%" %ACCOUNT_ARG% --list-courses"`) do (
    set /a N+=1
    set "C%%a=%%c"
    echo     [%%a] %%b	%%d
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
echo    ^(课程后面的「已完成/进行中」和分数来自学习进度页；
echo     显示「?」表示没取到，不影响选择^)
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
