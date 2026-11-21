param(
    [string]$Url,
    [string]$Mode,
    [int]$Max = -1,
    [string]$Interval,
    [string]$Path,
    [switch]$Headful,
    [switch]$NoPause
)

try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$Base = Split-Path -Parent $MyInvocation.MyCommand.Path
$Py = Join-Path $Base ".venv\Scripts\python.exe"
$Script = Join-Path $Base "douyin_batch.py"
$CookieFile = Join-Path $Base "cookie.txt"
if (-not $Path) { $Path = Join-Path $Base "Download" }

$ValidModes = @("post", "like", "collection", "mix", "one")
$Menu = @(
    @{ Key = "1"; Name = "post";       Desc = "主页作品（默认）" },
    @{ Key = "2"; Name = "like";       Desc = "喜欢 / 点赞过的作品" },
    @{ Key = "3"; Name = "collection"; Desc = "我的收藏" },
    @{ Key = "4"; Name = "mix";        Desc = "合集 / 播放列表" },
    @{ Key = "5"; Name = "one";        Desc = "单条作品" }
)

function Read-Mode {
    Write-Host ""
    Write-Host "  请选择下载模式：" -ForegroundColor Cyan
    foreach ($m in $Menu) {
        Write-Host ("    {0}) {1,-11} {2}" -f $m.Key, $m.Name, $m.Desc)
    }
    $sel = Read-Host "  输入序号或模式名，直接回车 = post"
    if ([string]::IsNullOrWhiteSpace($sel)) { return "post" }
    $sel = $sel.Trim().ToLower()
    $hit = $Menu | Where-Object { $_.Key -eq $sel }
    if ($hit) { return $hit.Name }
    if ($ValidModes -contains $sel) { return $sel }
    return $null
}

if (-not (Test-Path $Py) -or -not (Test-Path $Script)) {
    Write-Host "下载引擎缺失，请重新执行安装步骤。" -ForegroundColor Red
    if (-not $NoPause) { Read-Host "按回车退出" | Out-Null }
    return
}

$interactive = [string]::IsNullOrWhiteSpace($Url)

Write-Host ""
Write-Host "  抖音批量下载" -ForegroundColor Cyan
Write-Host ""

if ($interactive) {
    $Url = Read-Host "粘贴抖音链接（用户主页 / 作品 / 合集）"
    if ([string]::IsNullOrWhiteSpace($Url)) {
        Write-Host "未提供链接，已退出。" -ForegroundColor Yellow
        if (-not $NoPause) { Read-Host "按回车退出" | Out-Null }
        return
    }
    $Url = $Url.Trim()

    $Mode = Read-Mode
    if (-not $Mode) {
        Write-Host "模式无效，已退出。可选：$($ValidModes -join ' / ')" -ForegroundColor Red
        if (-not $NoPause) { Read-Host "按回车退出" | Out-Null }
        return
    }

    $maxIn = Read-Host "最多下载多少条（0 = 不限，直接回车 = 20）"
    if ([string]::IsNullOrWhiteSpace($maxIn)) {
        $Max = 20
    } else {
        $parsed = 0
        if (-not [int]::TryParse($maxIn.Trim(), [ref]$parsed) -or $parsed -lt 0) {
            Write-Host "条数必须是 0 或正整数，已退出。" -ForegroundColor Red
            if (-not $NoPause) { Read-Host "按回车退出" | Out-Null }
            return
        }
        $Max = $parsed
    }

    $ivIn = Read-Host "发布时间区间（格式 2025-01-01|2026-11-01，直接回车 = 全部）"
    if ([string]::IsNullOrWhiteSpace($ivIn)) {
        $Interval = "all"
    } else {
        $Interval = $ivIn.Trim()
    }
} else {
    $Url = $Url.Trim()
    if ([string]::IsNullOrWhiteSpace($Mode)) { $Mode = "post" }
    if ($Max -lt 0) { $Max = 0 }
    if ([string]::IsNullOrWhiteSpace($Interval)) { $Interval = "all" }
}

$Mode = $Mode.Trim().ToLower()
if ($ValidModes -notcontains $Mode) {
    Write-Host "模式 [$Mode] 无效。可选：$($ValidModes -join ' / ')" -ForegroundColor Red
    Write-Host "提示：条数请用 -Max，例如 -Mode like -Max 20" -ForegroundColor Yellow
    if (-not $NoPause) { Read-Host "按回车退出" | Out-Null }
    return
}

if ($Url -notmatch "douyin\.com") {
    Write-Host "这不是抖音链接：$Url" -ForegroundColor Red
    if (-not $NoPause) { Read-Host "按回车退出" | Out-Null }
    return
}

if ($Interval -ne "all" -and $Interval -notmatch "^\d{4}-\d{2}-\d{2}\|\d{4}-\d{2}-\d{2}$") {
    Write-Host "时间区间格式不对，应为 2025-01-01|2026-11-01 或 all。" -ForegroundColor Red
    if (-not $NoPause) { Read-Host "按回车退出" | Out-Null }
    return
}

$cookie = ""
if (Test-Path $CookieFile) { $cookie = Get-Content $CookieFile -Raw -ErrorAction SilentlyContinue }
if ($cookie) { $cookie = ($cookie -replace "[\r\n]", "").Trim() }
if (-not $cookie -or $cookie.Length -lt 20) {
    Write-Host "cookie.txt 为空或内容过短，请先双击「设置Cookie.cmd」粘贴 Cookie。" -ForegroundColor Red
    if (-not $NoPause) { Read-Host "按回车退出" | Out-Null }
    return
}

$maxText = if ($Max -eq 0) { "不限" } else { "$Max 条" }
Write-Host ""
Write-Host "链接：$Url"
Write-Host "模式：$Mode    条数：$maxText    区间：$Interval"
Write-Host "保存：$Path" -ForegroundColor DarkGray
Write-Host ""

$env:PYTHONIOENCODING = "utf-8"
$pyArgs = @($Script, "--url", $Url, "--mode", $Mode, "--max", "$Max",
            "--interval", $Interval, "--out", $Path, "--cookie", $CookieFile)
if ($Headful) { $pyArgs += "--headful" }

& $Py @pyArgs
$code = $LASTEXITCODE

Write-Host ""
switch ($code) {
    0 { Write-Host "全部完成，文件保存在：$Path" -ForegroundColor Green }
    3 { Write-Host "部分文件下载失败，其余已完成。可重新运行补齐（已下载的会自动跳过）。" -ForegroundColor Yellow }
    2 { Write-Host "参数错误，请检查链接与模式是否匹配。" -ForegroundColor Yellow }
    default {
        Write-Host "任务未正常结束（退出码 $code）。按顺序排查：" -ForegroundColor Yellow
        Write-Host "  1) Cookie 失效 → 双击「设置Cookie.cmd」重设"
        Write-Host "  2) 页面需人工验证 → 加 -Headful 参数重跑，看窗口里发生了什么"
        Write-Host "  3) 平台改版 → 等待脚本更新"
    }
}

if (-not $NoPause) { Read-Host "按回车关闭窗口" | Out-Null }
