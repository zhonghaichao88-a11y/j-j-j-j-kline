# Lead Radar 一键启动：自动安装 Python、依赖和浏览器组件，启动服务并打开网页。
# 由 start.bat 调用；install-browser.bat 用 -RepairBrowser 重新安装浏览器组件。
param([switch]$RepairBrowser)

$ErrorActionPreference = 'Continue'   # 原生命令的退出码逐个检查；PowerShell 5.1 下 Stop 会误判 pip 的警告输出
$ProgressPreference = 'SilentlyContinue'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}

$Root = $PSScriptRoot
Set-Location -LiteralPath $Root
try { $Host.UI.RawUI.WindowTitle = 'Lead Radar 启动中…' } catch {}
New-Item -ItemType Directory -Force -Path (Join-Path $Root 'data') | Out-Null
try { Start-Transcript -Path (Join-Path $Root 'data\start.log') -Force | Out-Null } catch {}

$PythonVersion = '3.12.10'
$PipMirror = 'https://pypi.tuna.tsinghua.edu.cn/simple'
$PlaywrightMirror = 'https://npmmirror.com/mirrors/playwright'
$VenvDir = Join-Path $Root '.venv'
$VenvPy = Join-Path $VenvDir 'Scripts\python.exe'

function Step($n, $text) { Write-Host ''; Write-Host "[$n/6] $text" -ForegroundColor Cyan }
function Info($text) { Write-Host "      $text" }
function Ok($text) { Write-Host "      $text" -ForegroundColor Green }
function Warn($text) { Write-Host "      $text" -ForegroundColor Yellow }

function Get-EnvValue($name, $default) {
    $file = Join-Path $Root '.env'
    if (Test-Path -LiteralPath $file) {
        foreach ($line in Get-Content -LiteralPath $file -Encoding UTF8) {
            if ($line -match "^\s*$name\s*=\s*(.*?)\s*$" -and $Matches[1]) { return $Matches[1] }
        }
    }
    return $default
}

function Test-Python($exe) {
    if (-not $exe -or -not (Test-Path -LiteralPath $exe)) { return $false }
    if ($exe -like '*\WindowsApps\*') { return $false }   # 微软商店的占位 python.exe
    & $exe -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Find-Python {
    $candidates = New-Object System.Collections.Generic.List[string]
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($v in '-3.13', '-3.12', '-3.11', '-3') {
            $out = & py $v -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $out) { $candidates.Add(([string]($out | Select-Object -First 1)).Trim()) }
        }
    }
    foreach ($name in 'python', 'python3') {
        foreach ($cmd in @(Get-Command $name -All -ErrorAction SilentlyContinue)) { $candidates.Add($cmd.Source) }
    }
    foreach ($v in '313', '312', '311') {
        if ($env:LOCALAPPDATA) { $candidates.Add((Join-Path $env:LOCALAPPDATA "Programs\Python\Python$v\python.exe")) }
        if ($env:ProgramFiles) { $candidates.Add((Join-Path $env:ProgramFiles "Python$v\python.exe")) }
    }
    foreach ($c in $candidates) { if (Test-Python $c) { return $c } }
    return $null
}

function Install-Python {
    $file = if ([Environment]::Is64BitOperatingSystem) { "python-$PythonVersion-amd64.exe" } else { "python-$PythonVersion.exe" }
    $tempDir = if ($env:TEMP) { $env:TEMP } else { [System.IO.Path]::GetTempPath() }
    $dest = Join-Path $tempDir $file
    $urls = @("https://www.python.org/ftp/python/$PythonVersion/$file",
              "https://registry.npmmirror.com/-/binary/python/$PythonVersion/$file")
    $downloaded = $false
    foreach ($u in $urls) {
        try {
            Info "正在下载 Python $PythonVersion（约 25MB）：$u"
            Invoke-WebRequest -Uri $u -OutFile $dest -UseBasicParsing -TimeoutSec 900 -ErrorAction Stop
            if ((Get-Item -LiteralPath $dest).Length -gt 10MB) { $downloaded = $true; break }
        } catch { Warn "这个地址下载失败：$($_.Exception.Message)" }
    }
    if (-not $downloaded) { throw "Python 下载失败。请检查网络，或手动安装 Python 3.12（https://www.python.org/downloads/windows/ ，安装时勾选 Add Python to PATH）后再双击 start.bat" }
    Info '正在静默安装 Python（只装给当前用户，约 1-3 分钟，请稍候）…'
    $p = Start-Process -FilePath $dest -ArgumentList '/quiet', 'InstallAllUsers=0', 'PrependPath=1', 'Include_launcher=1', 'Include_test=0', 'Shortcuts=0' -Wait -PassThru
    if ($p.ExitCode -ne 0) { throw "Python 安装失败（安装程序返回 $($p.ExitCode)）。可以手动运行 $dest 安装后再试" }
    Remove-Item -LiteralPath $dest -Force -ErrorAction SilentlyContinue
}

function Pip-Install([string[]]$PackageArgs) {
    & $VenvPy -m pip install --disable-pip-version-check @PackageArgs
    if ($LASTEXITCODE -eq 0) { return }
    Warn '官方源安装失败，改用国内镜像（清华）重试…'
    & $VenvPy -m pip install --disable-pip-version-check -i $PipMirror @PackageArgs
    if ($LASTEXITCODE -ne 0) { throw '安装依赖失败，请检查网络后重新双击 start.bat' }
}

function Find-SystemBrowser {
    foreach ($p in @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
                     "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
                     "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
                     "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe")) {
        if ($p -and (Test-Path -LiteralPath $p)) { return $p }
    }
    return $null
}

function Get-LocalBuild {
    # Must match BUILD_ID in app/main.py: sha256 over these files' bytes, first 16 hex chars.
    $files = 'app\main.py', 'app\browser_collector.py', 'app\adapters.py', 'app\netcheck.py', 'app\relay.py', 'static\index.html'
    $ms = New-Object System.IO.MemoryStream
    foreach ($f in $files) {
        $path = Join-Path $Root $f
        if (Test-Path -LiteralPath $path) { $bytes = [System.IO.File]::ReadAllBytes($path); $ms.Write($bytes, 0, $bytes.Length) }
    }
    $hash = [System.Security.Cryptography.SHA256]::Create().ComputeHash($ms.ToArray())
    return (-join ($hash | ForEach-Object { $_.ToString('x2') })).Substring(0, 16)
}

function Stop-PortOwner($port, $url) {
    $ids = @()
    try {
        $ids = @(Get-NetTCPConnection -LocalPort ([int]$port) -State Listen -ErrorAction Stop | Select-Object -ExpandProperty OwningProcess -Unique)
    } catch {
        if (-not $env:SystemRoot) { return $false }
        $netstat = Join-Path $env:SystemRoot 'System32\NETSTAT.EXE'
        $ids = @(& $netstat -ano | Select-String -Pattern "^\s*TCP\s+\S+:$port\s+\S+\s+LISTENING\s+(\d+)" | ForEach-Object { [int]$_.Matches[0].Groups[1].Value } | Select-Object -Unique)
    }
    foreach ($id in $ids) {
        $proc = Get-Process -Id $id -ErrorAction SilentlyContinue
        if ($proc -and $proc.ProcessName -match '^python') { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }
    }
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 500
        try { Invoke-RestMethod -Uri "$url/api/health" -TimeoutSec 1 -ErrorAction Stop | Out-Null } catch { return $true }
    }
    return $false
}

$exitCode = 0
try {
    Write-Host '=============================================' -ForegroundColor Cyan
    Write-Host '   Lead Radar 一键启动（首次会自动安装所需组件）' -ForegroundColor Cyan
    Write-Host '=============================================' -ForegroundColor Cyan
    if ($Root -match '\\(Temp|Tmp)\\' -and $Root -match '(\.zip|\.rar|\.7z|Rar\$)') {
        throw '看起来你是在压缩软件里直接运行的。请先把 ZIP 完整解压到一个文件夹（例如 D:\LeadRadar），再双击里面的 start.bat'
    }
    if (-not (Test-Path -LiteralPath (Join-Path $Root '.env')) -and (Test-Path -LiteralPath (Join-Path $Root '.env.example'))) {
        Copy-Item -LiteralPath (Join-Path $Root '.env.example') -Destination (Join-Path $Root '.env')
    }
    $AppVersion = ''
    $m = Select-String -LiteralPath (Join-Path $Root 'app\main.py') -Pattern 'FastAPI\(.*version="([0-9.]+)"' | Select-Object -First 1
    if ($m) { $AppVersion = $m.Matches[0].Groups[1].Value }
    $Port = Get-EnvValue 'APP_PORT' '8000'
    $Url = "http://127.0.0.1:$Port"

    # ---- 已经在运行：只有「就是这个文件夹、而且是最新代码」才直接打开网页；
    #      否则是旧版本/另一个文件夹的程序占着端口，自动把它关掉再启动新版
    $health = $null
    try { $health = Invoke-RestMethod -Uri "$Url/api/health" -TimeoutSec 2 -ErrorAction Stop } catch {}
    if ($health) {
        $sameFolder = $false
        if ($health.root) {
            try { $sameFolder = ([System.IO.Path]::GetFullPath([string]$health.root).TrimEnd('\') -ieq $Root.TrimEnd('\')) } catch {}
        }
        if ($sameFolder -and $health.build -and ($health.build -eq (Get-LocalBuild))) {
            Ok '程序已经在运行（就是这个文件夹的最新代码），直接打开网页。'
            Start-Process $Url
            return
        }
        $where = if ($health.root) { [string]$health.root } else { '另一个文件夹' }
        Warn "端口 $Port 上运行的是旧的 Lead Radar（$where，版本 $($health.version)），正在自动关闭它…"
        if (-not (Stop-PortOwner $Port $Url)) {
            throw '没能自动关闭旧程序。请关闭任务栏里所有「Lead Radar Server」窗口（或运行旧文件夹里的 stop.bat），再双击 start.bat'
        }
        Ok '旧程序已关闭，接下来启动这个文件夹里的新版本'
    }

    Step 1 '检查 Python'
    if (Test-Python $VenvPy) {
        Ok '运行环境已就绪'
    } else {
        $BasePy = Find-Python
        if (-not $BasePy) {
            Warn '没有找到 Python 3.11 以上版本，开始自动安装…'
            Install-Python
            $BasePy = Find-Python
            if (-not $BasePy) { throw 'Python 已安装但没找到，请关闭这个窗口后重新双击 start.bat' }
        }
        Ok "使用 Python：$BasePy"
        Step 2 '创建程序运行环境'
        if (Test-Path -LiteralPath $VenvDir) { Remove-Item -LiteralPath $VenvDir -Recurse -Force }   # 已损坏（例如文件夹被移动过）
        & $BasePy -m venv $VenvDir
        if ($LASTEXITCODE -ne 0 -or -not (Test-Python $VenvPy)) { throw '创建运行环境失败。请确认文件夹路径可写，然后重试' }
        Ok '已创建'
    }

    Step 3 '检查依赖'
    $reqFile = Join-Path $Root 'requirements.txt'
    $reqHash = (Get-FileHash -LiteralPath $reqFile -Algorithm SHA256).Hash
    $reqMarker = Join-Path $VenvDir '.requirements-hash'
    $installedHash = if (Test-Path -LiteralPath $reqMarker) { (Get-Content -LiteralPath $reqMarker -Raw).Trim() } else { '' }
    & $VenvPy -c "import fastapi, uvicorn, requests, sqlalchemy, dotenv, playwright" 2>$null
    if ($LASTEXITCODE -ne 0 -or $installedHash -ne $reqHash) {
        Info '正在安装依赖（首次约 1-3 分钟）…'
        Pip-Install @('-r', $reqFile, 'playwright>=1.55,<2')
        Set-Content -LiteralPath $reqMarker -Value $reqHash
    }
    Ok '依赖已就绪'

    Step 4 '检查采集浏览器'
    $browserMarker = Join-Path $VenvDir '.browser-ready'
    if ($RepairBrowser -or -not (Test-Path -LiteralPath $browserMarker)) {
        Info '正在安装浏览器组件 Chromium（约 150MB，只需一次）…'
        & $VenvPy -m playwright install chromium
        if ($LASTEXITCODE -ne 0) {
            Warn '官方地址下载失败，改用国内镜像重试…'
            $env:PLAYWRIGHT_DOWNLOAD_HOST = $PlaywrightMirror
            & $VenvPy -m playwright install chromium
            Remove-Item Env:PLAYWRIGHT_DOWNLOAD_HOST -ErrorAction SilentlyContinue
        }
        if ($LASTEXITCODE -eq 0) {
            Set-Content -LiteralPath $browserMarker -Value 'chromium'
            Ok 'Chromium 已安装'
        } elseif (Find-SystemBrowser) {
            Set-Content -LiteralPath $browserMarker -Value 'system'
            Warn "Chromium 没装上，改用电脑自带的浏览器：$(Find-SystemBrowser)（功能一样）"
        } else {
            Warn '浏览器组件没装上，社交平台采集暂时不能用（公开网站搜索不受影响）。下次启动会自动重试'
        }
    } else {
        Ok '已就绪'
    }

    Step 5 '桌面快捷方式'
    try {
        $desktop = [Environment]::GetFolderPath('Desktop')
        $lnkPath = Join-Path $desktop 'Lead Radar.lnk'
        $target = Join-Path $Root 'start.bat'
        $shell = New-Object -ComObject WScript.Shell
        $needs = -not (Test-Path -LiteralPath $lnkPath)
        if (-not $needs) { $needs = ($shell.CreateShortcut($lnkPath).TargetPath -ne $target) }
        if ($needs) {
            $lnk = $shell.CreateShortcut($lnkPath)
            $lnk.TargetPath = $target
            $lnk.WorkingDirectory = $Root
            $lnk.IconLocation = "$env:SystemRoot\System32\shell32.dll,13"
            $lnk.Description = 'Lead Radar 一键启动'
            $lnk.Save()
            Ok '已在桌面创建「Lead Radar」快捷方式，以后双击它就能启动'
        } else { Ok '已存在' }
    } catch { Warn '没能创建桌面快捷方式（不影响使用）' }

    Step 6 '启动服务'
    $listener = $null
    try {
        $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, [int]$Port)
        $listener.Start()
    } catch {
        throw "端口 $Port 被其他程序占用。请关闭占用它的程序，或在 .env 里把 APP_PORT 改成别的端口（例如 8010）"
    } finally { if ($listener) { $listener.Stop() } }
    Start-Process -FilePath (Join-Path $Root 'run-server.bat') -WorkingDirectory $Root -WindowStyle Minimized
    $ready = $false
    for ($i = 0; $i -lt 90; $i++) {
        Start-Sleep -Seconds 1
        try { $h = Invoke-RestMethod -Uri "$Url/api/health" -TimeoutSec 2 -ErrorAction Stop; if ($h.ok) { $ready = $true; break } } catch {}
    }
    if (-not $ready) { throw '服务没有按时启动。请点开任务栏里的「Lead Radar Server」窗口查看报错' }
    Ok "已启动：$Url"
    Start-Process $Url
    Write-Host ''
    Write-Host '  完成！网页已经打开。' -ForegroundColor Green
    Write-Host '  · 任务栏里最小化的「Lead Radar Server」窗口就是程序本身，关掉它程序就停止。' -ForegroundColor Green
    Write-Host '  · 以后双击桌面上的「Lead Radar」即可。这个窗口 8 秒后自动关闭。' -ForegroundColor Green
    Start-Sleep -Seconds 8
} catch {
    $exitCode = 1
    Write-Host ''
    Write-Host "启动失败：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host "详细记录保存在：$(Join-Path $Root 'data\start.log')" -ForegroundColor Yellow
} finally {
    try { Stop-Transcript | Out-Null } catch {}
}
exit $exitCode
