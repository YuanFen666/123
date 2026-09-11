# -*- coding: utf-8 -*-
<#
.SYNOPSIS
    把当前工作区打包发布到 GitHub 仓库（自带密钥防泄漏闸门）。

.DESCRIPTION
    流程：克隆仓库到临时目录 -> 按白名单拷贝源码/文档/工具（排除 exe、日志、配置、
    cookies、备份）-> 自动脱敏源码里硬编码的 token -> 全树密钥扫描 ->
    扫描通过才提交推送。

    为什么要这么麻烦：本目录里的 config.ini 含学习通账号密码、ANEVOL token、
    DeepSeek key；源码 anevol_bridge.py 里还硬编码过 token。仓库是公开的，
    推错一次密钥就永久泄漏了。

.EXAMPLE
    pwsh -File 工具脚本\publish_to_github.ps1              # 只打包+扫描+本地提交，不推送
    pwsh -File 工具脚本\publish_to_github.ps1 -Push        # 打包并推送到 GitHub
    pwsh -File 工具脚本\publish_to_github.ps1 -Push -Message "fix: xxx"
#>
param(
    [switch]$Push,
    [string]$RepoUrl = "https://github.com/YuanFen666/123.git",
    [string]$Message = ""
)

$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
# 注意：这里刻意不用 'Stop'。Windows PowerShell 5.1 会把原生命令（git/robocopy）写到
# stderr 的正常输出当成致命错误，配 'Stop' 会让脚本无故中断。所以文件操作显式加
# -ErrorAction Stop，原生命令逐个检查 $LASTEXITCODE。

$work = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)   # 网课\
$stage = Join-Path $env:TEMP "chaoxing_publish"
$repoDir = Join-Path $stage "repo"

Write-Host "工作区: $work" -ForegroundColor Cyan
Write-Host "暂存区: $repoDir" -ForegroundColor Cyan

# ---------------------------------------------------------------- 1. 克隆
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force -ErrorAction Stop }
New-Item -ItemType Directory -Path $stage -Force -ErrorAction Stop | Out-Null
$env:GIT_TERMINAL_PROMPT = '0'
git clone --depth 1 $RepoUrl $repoDir 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { throw "克隆仓库失败: $RepoUrl" }
Get-ChildItem $repoDir -Force | Where-Object { $_.Name -ne '.git' } |
    Remove-Item -Recurse -Force -ErrorAction Stop

# ---------------------------------------------------------------- 2. 拷贝
function Copy-Tree([string]$src, [string]$dst, [string[]]$xf) {
    if (-not (Test-Path $src)) { Write-Host "  跳过（不存在）: $src" -ForegroundColor Yellow; return }
    $a = @($src, $dst, '/E', '/XD', 'build', 'dist', '__pycache__', '.git', '/XF') + $xf +
         @('/NFL', '/NDL', '/NJH', '/NJS', '/R:1', '/W:1')
    & robocopy @a | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy 失败: $src (exit $LASTEXITCODE)" }
}

Write-Host "`n[1/4] 拷贝文件..." -ForegroundColor Green
Copy-Tree "$work\源码\chaoxing-fixed" "$repoDir\源码\chaoxing-fixed" `
    @('*.pyc', '*.log', 'cache.json', 'cookies.txt', 'config.ini', '*.exe')
Copy-Tree "$work\源码\anevol-bridge" "$repoDir\源码\anevol-bridge" `
    @('*.pyc', '*.log', '*.exe', 'anevol_token.txt')
Copy-Tree "$work\工具脚本" "$repoDir\工具脚本" @('*.pyc')
foreach ($f in @('说明文档.md', 'README.md', '.gitignore', 'start_chaoxing.bat')) {
    if (Test-Path "$work\$f") { Copy-Item "$work\$f" "$repoDir\$f" -Force }
}

# 【重要】所有写出都必须用「无 BOM 的 UTF-8」：
# PowerShell 5.1 的 Set-Content -Encoding UTF8 会加 BOM，而带 BOM 的 ini 会让
# configparser 抛 MissingSectionHeaderError（程序一启动就崩）。所以统一走这个函数。
function Write-Utf8NoBom([string]$path, [string]$text) {
    [System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding($false)))
}

# config.ini -> 脱敏的 config.ini.example
if (Test-Path "$work\config.ini") {
    $blank = '^(username|password|tokens|key|siliconflow_key|icodef_authorization)\s*='
    $head = @(
        '; ============================================================================',
        ';  配置模板：复制成 config.ini 再填自己的值',
        ';  【重要】config.ini 含账号密码与各种 token，已被 .gitignore 忽略，',
        ';          千万不要提交到公开仓库（本文件是脱敏模板，可以提交）。',
        '; ============================================================================',
        ''
    )
    $body = Get-Content "$work\config.ini" -Encoding UTF8 | ForEach-Object {
        if ($_ -match $blank) { "$($Matches[1]) = " } else { $_ }
    }
    Write-Utf8NoBom "$repoDir\config.ini.example" (($head + $body) -join "`r`n")
}

# ---------------------------------------------------------------- 3. 脱敏 + 扫描
Write-Host "[2/4] 源码脱敏..." -ForegroundColor Green
$bridge = "$repoDir\源码\anevol-bridge\anevol_bridge.py"
if (Test-Path $bridge) {
    $t = Get-Content $bridge -Raw -Encoding UTF8
    $t = $t -replace 'ANEVOL_TOKEN\s*=\s*"[^"]*"', 'ANEVOL_TOKEN = ""'
    Write-Utf8NoBom $bridge $t
}
$diag = "$repoDir\源码\anevol-bridge\_diag.py"
if (Test-Path $diag) {
    $t = Get-Content $diag -Raw -Encoding UTF8
    $t = $t -replace 'TOKEN\s*=\s*"[^"]*"', 'TOKEN = "在这里填你的 ANEVOL token（不要提交真实值）"'
    Write-Utf8NoBom $diag $t
}
# 文档/探针结果里的 token 前后缀、完整 token
Get-ChildItem $repoDir -Recurse -File -Include *.md, *.txt, *.py, *.ini | ForEach-Object {
    $t = Get-Content $_.FullName -Raw -Encoding UTF8
    $o = $t
    $t = $t -replace '[0-9a-fA-F]{8}(…|\.\.\.)[0-9a-fA-F]{8}', '<你的ANEVOL-token>'
    $t = $t -replace 'e1aa9a06', 'xxxxxxxx'
    if ($t -ne $o) { Write-Utf8NoBom $_.FullName $t }
}

Write-Host "[3/4] 密钥扫描..." -ForegroundColor Green
# 只查「真正的密钥」：完整的 64 位 hex、OpenAI 风格 key、带引号的字面量口令、11 位手机号。
# 刻意不查 `username = common_config.get(...)` 这种代码，否则满屏误报。
$rules = [ordered]@{
    '64位十六进制串(疑似 token)' = '\b[0-9a-fA-F]{64}\b'
    'OpenAI 风格 key'            = 'sk-[A-Za-z0-9_\-]{20,}'
    '硬编码的字面量口令'          = '(?i)\b(password|passwd|pwd|token|secret|api_?key)\s*[:=]\s*["''][^"''\s]{6,}["'']'
    '11 位手机号'                = '\b1[3-9]\d{9}\b'
}
# 这些是占位符/示例，不是真实密钥
$allow = @('填', '你的', 'xxx', 'XXX', '<', '示例', 'example', '请', 'placeholder', '在这里',
           '13800000000', '13800138000')   # 国内标准示例号码，不是真号
$files = Get-ChildItem $repoDir -Recurse -File -Force | Where-Object {
    $_.FullName -notmatch '\\\.git\\' -and $_.Extension -notin @('.json', '.bat')
}
$problems = @()
foreach ($f in $files) {
    foreach ($name in $rules.Keys) {
        $hit = Select-String -LiteralPath $f.FullName -Pattern $rules[$name] -ErrorAction SilentlyContinue
        foreach ($h in $hit) {
            # config.ini.example 里的空赋值不算问题
            if ($f.Name -eq 'config.ini.example' -and $h.Line -match '=\s*$') { continue }
            $skip = $false
            foreach ($a in $allow) { if ($h.Line -like "*$a*") { $skip = $true; break } }
            if ($skip) { continue }
            $problems += "  [$name] $($f.FullName.Replace($repoDir,'.')):$($h.LineNumber)  $($h.Line.Trim())"
        }
    }
}
if ($problems.Count -gt 0) {
    Write-Host "`n!! 扫描到疑似密钥，已中止，未推送：`n" -ForegroundColor Red
    $problems | Select-Object -First 20 | ForEach-Object { Write-Host $_ -ForegroundColor Red }
    exit 1
}
Write-Host "  扫描通过：未发现密钥" -ForegroundColor Green

# ---------------------------------------------------------------- 4. 提交 / 推送
Write-Host "[4/4] 提交..." -ForegroundColor Green
git -C $repoDir add -A 2>&1 | Out-Null
git -C $repoDir diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
    if (-not $Message) { $Message = "chore: 同步更新 $(Get-Date -Format 'yyyy-MM-dd HH:mm')" }
    git -C $repoDir commit -q -m $Message 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "本地提交失败" }
    Write-Host "  已提交: $Message"
} else {
    Write-Host "  没有改动，无需提交" -ForegroundColor Yellow
}

if ($Push) {
    if ($env:GIT_PUBLISH_PAT) {
        # 用一次性 PAT 推送，不写进 .git/config
        $u = $RepoUrl -replace '^https://', "https://$($env:GIT_PUBLISH_PAT)@"
        git -C $repoDir push $u main 2>&1 | Out-Null
    } else {
        git -C $repoDir push origin main 2>&1 | Out-Null
    }
    if ($LASTEXITCODE -ne 0) { throw "推送失败（检查 GitHub 凭据）" }
    Write-Host "`n已推送到 $RepoUrl" -ForegroundColor Green
} else {
    Write-Host "`n（未推送。确认无误后加 -Push 再跑一次）" -ForegroundColor Yellow
    Write-Host "暂存目录: $repoDir" -ForegroundColor Cyan
}
