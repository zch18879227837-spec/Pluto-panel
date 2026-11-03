#requires -Version 5.1

<#

    打包 AstrBot 插件市场 zip

    - 根目录固定为 astrbot_plugin_token_panel

    - 自动排除运行期垃圾：__pycache__ / data / 日志 / 用户数据

    用法：

        powershell -NoProfile -ExecutionPolicy Bypass -File tools\pack.ps1

        powershell ... -File tools\pack.ps1 -Root "D:\...\astrbot_plugin_token_panel"

#>

param(

    [string]$Root = "",

    [string]$OutDir = ""

)



$ErrorActionPreference = "Stop"

$PluginName = "astrbot_plugin_token_panel"



if (-not $Root) {

    # tools\pack.ps1 -> 上一级即插件根目录

    $Root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)

}

if (-not (Test-Path (Join-Path $Root "metadata.yaml"))) {

    Write-Host ""

    Write-Host "  [错误] 没找到 metadata.yaml，插件根目录不对：" -ForegroundColor Red

    Write-Host "         $Root" -ForegroundColor Red

    exit 1

}

# v1.16.3：默认输出到插件目录之外，避免 dist/*.zip 被误打包进上传包
if (-not $OutDir) { $OutDir = Join-Path (Split-Path -Parent $Root) "release" }

if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir | Out-Null }



$DriveRoot = [System.IO.Path]::GetPathRoot($Root)

$FreeGB = [math]::Round((Get-PSDrive -Name $DriveRoot.Substring(0, 1)).Free / 1GB, 1)



$stage = Join-Path $env:TEMP ("pluto-pack-" + [guid]::NewGuid().ToString("N"))

$target = Join-Path $stage $PluginName

New-Item -ItemType Directory -Path $target -Force | Out-Null



$skipDirs = @("__pycache__", "data", "dist", ".git", ".github", ".venv", "venv", "carry_backups", "tools", "docs", "douyin_captures", "plugin_data", "node_modules", ".pytest_cache", ".mypy_cache", "tests")

$skipFiles = @("settings.json", "session_meta.json", "compress_log.json")

$skipFilePatterns = @("更新说明-*.md", "*.bak", "*.bak-*")



Write-Host ""

Write-Host "  插件目录 : $Root"

Write-Host "  剩余空间 : $FreeGB GB"

Write-Host "  暂存目录 : $stage"

Write-Host ""



$count = 0

Get-ChildItem -Path $Root -Recurse -Force | ForEach-Object {

    $rel = $_.FullName.Substring($Root.Length).TrimStart("\")

    $parts = $rel -split "\\"

    if ($parts[0] -in $skipDirs) { return }

    if ($_.PSIsContainer) { return }

    if ($_.Name -in $skipFiles) { return }

    foreach ($pat in $skipFilePatterns) { if ($_.Name -like $pat) { return } }

    if ($_.Name -like "*.bak-*" -or $_.Name -like "*.bak") { return }

    if ($_.Extension -in @(".log", ".zip", ".pyc")) { return }

    $dest = Join-Path $target $rel

    $destDir = Split-Path -Parent $dest

    if (-not (Test-Path $destDir)) { New-Item -ItemType Directory -Path $destDir -Force | Out-Null }

    Copy-Item -LiteralPath $_.FullName -Destination $dest -Force

    $script:count++

}



Write-Host "  已收集 $count 个文件，开始压缩..." -ForegroundColor Cyan



$out = Join-Path $OutDir "$PluginName.zip"

if (Test-Path $out) { Remove-Item $out -Force }

Compress-Archive -Path $target -DestinationPath $out -CompressionLevel Optimal



# ---- 校验 ----

Add-Type -AssemblyName System.IO.Compression.FileSystem

$zip = [System.IO.Compression.ZipFile]::OpenRead($out)

try {

    # .NET 在 Windows 上给的条目名用反斜杠，统一成 / 再校验

    $entries = $zip.Entries | ForEach-Object { ($_.FullName -replace '\\', '/').TrimEnd('/') }

} finally {

    $zip.Dispose()

}



$badRoot = $entries | Where-Object { -not $_.StartsWith("$PluginName/") }

$hasMeta = $entries -contains "$PluginName/metadata.yaml"

$hasMain = $entries -contains "$PluginName/main.py"

$hasView = $entries -contains "$PluginName/views/dashboard/index.html"



Write-Host ""

# ---- v1.16.3：垃圾/敏感文件硬性拦截（插件市场审核要求）----
$forbiddenPattern = '(?i)(__pycache__|/dist/|\.pyc$|\.bak|cmd_config\.json|/data/|plugin_data/|douyin_captures/|\.db$|\.log$|\.jsonl$|/settings\.json$|\.zip$)'
$forbidden = $entries | Where-Object { $_ -match $forbiddenPattern }

if ($forbidden.Count -gt 0) {

    Write-Host ""

    Write-Host "  [失败] 包里混进了不该发布的文件（缓存/备份/运行数据/旧构建）" -ForegroundColor Red

    $forbidden | Select-Object -First 8 | ForEach-Object { Write-Host "         $_" -ForegroundColor Red }

    Write-Host "         请把插件目录里的 __pycache__、*.bak*、dist/、data/、plugin_data/ 清掉后重打。" -ForegroundColor Yellow

    exit 1

}

if ($badRoot.Count -gt 0 -or -not $hasMeta -or -not $hasMain) {

    Write-Host "  [失败] zip 结构不符合插件市场要求" -ForegroundColor Red

    if ($badRoot.Count -gt 0) { Write-Host "         存在根目录外的条目，例如：$($badRoot[0])" -ForegroundColor Red }

    if (-not $hasMeta) { Write-Host "         缺少 $PluginName/metadata.yaml" -ForegroundColor Red }

    if (-not $hasMain) { Write-Host "         缺少 $PluginName/main.py" -ForegroundColor Red }

    exit 1

}



$viewMark = "缺失"

if ($hasView) { $viewMark = "OK" }



$sizeMB = [math]::Round((Get-Item $out).Length / 1MB, 2)

Write-Host "  [完成] $out" -ForegroundColor Green

Write-Host "         文件数 $count ｜ 体积 $sizeMB MB ｜ 根目录 $PluginName/" -ForegroundColor Green

Write-Host "         校验：metadata.yaml OK ｜ main.py OK ｜ views/dashboard/index.html $viewMark" -ForegroundColor Green

Write-Host "         无 __pycache__ / *.bak / dist / data / *.db / *.log 等垃圾文件" -ForegroundColor Green

Write-Host "         条目数 $($entries.Count)" -ForegroundColor Green

Write-Host ""

Write-Host "  下一步：登录插件市场，手动上传这个 zip 并粘贴简介。" -ForegroundColor Yellow

Write-Host ""



Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue