param(
    [Parameter(Mandatory=$true)][string]$BatPath,
    [Parameter(Mandatory=$true)][string]$JsonPath
)
$ErrorActionPreference = 'Stop'
$gbk = [System.Text.Encoding]::GetEncoding(936)
$spec = [System.IO.File]::ReadAllText($JsonPath, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
$t = $gbk.GetString([System.IO.File]::ReadAllBytes($BatPath))
foreach ($i in 1..9) {
    $o = $spec."old$i"; $n = $spec."new$i"
    if ($null -eq $o -or $null -eq $n) { continue }
    $cnt = ([regex]::Matches($t, [regex]::Escape($o))).Count
    if ($cnt -eq 1) { $t = $t.Replace($o, $n); "REPLACED old$i" }
    elseif ($cnt -eq 0) { "SKIP(not-found) old$i" }
    else { "SKIP(ambiguous x$cnt) old$i" }
}
[System.IO.File]::WriteAllBytes($BatPath, $gbk.GetBytes($t))
"SIZE " + (Get-Item $BatPath).Length
