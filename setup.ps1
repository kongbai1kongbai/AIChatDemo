# ============================================================
#  AIChatDemo 一键部署脚本 (远程主机版)
#  目标目录: D:\AI\code + D:\AI\workspace
#  模式: 远程 API (无需 GPU)
#
#  用法: 以管理员身份运行 PowerShell，执行:
#    Set-ExecutionPolicy Bypass -Scope Process -Force
#    .\setup.ps1
# ============================================================

$ErrorActionPreference = "Continue"
$AI_ROOT = "D:\AI"
$CODE_DIR = "$AI_ROOT\code"
$WS_DIR = "$AI_ROOT\workspace"
$REPO_URL = "git@github.com:kongbai1kongbai/AIChatDemo.git"

function Log($msg) {
    $ts = Get-Date -Format "HH:mm:ss"
    Write-Host "[$ts] $msg" -ForegroundColor Cyan
}

function Check-Admin {
    $current = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($current)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Wait-Key($msg = "按回车继续...") {
    Write-Host ""
    Write-Host $msg -ForegroundColor Yellow
    $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
    Write-Host ""
}

# ============================================================
# [0] 环境检查
# ============================================================
Write-Host ""
Write-Host "  ============================================" -ForegroundColor Green
Write-Host "    AIChatDemo 一键部署脚本" -ForegroundColor Green
Write-Host "  ============================================" -ForegroundColor Green
Write-Host ""

if (-not (Check-Admin)) {
    Write-Host "[错误] 请以管理员身份运行此脚本！" -ForegroundColor Red
    Write-Host "  右键 PowerShell -> 以管理员身份运行" -ForegroundColor Red
    exit 1
}

# 检查磁盘空间
$drive = Get-PSDrive -Name "D" -ErrorAction SilentlyContinue
if ($drive) {
    $freeGB = [math]::Round($drive.Free / 1GB, 1)
    Log "D: 盘剩余空间: ${freeGB}GB"
    if ($freeGB -lt 5) {
        Write-Host "[警告] D盘空间不足 5GB，可能不够用" -ForegroundColor Yellow
    }
} else {
    Write-Host "[警告] 未检测到 D: 盘，将使用当前盘符" -ForegroundColor Yellow
    $AI_ROOT = "C:\AI"
    $CODE_DIR = "$AI_ROOT\code"
    $WS_DIR = "$AI_ROOT\workspace"
}

# ============================================================
# [1/6] 安装 Git
# ============================================================
Log "[1/6] 检查 Git..."

if (Get-Command git -ErrorAction SilentlyContinue) {
    $gitVer = git --version
    Log "Git 已安装: $gitVer"
} else {
    Log "正在安装 Git..."
    winget install --id Git.Git -e --accept-source-agreements --accept-package-agreements --silent
    # 刷新 PATH
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
    if (Get-Command git -ErrorAction SilentlyContinue) {
        Log "Git 安装成功"
    } else {
        Write-Host "[错误] Git 安装失败，请手动安装: https://git-scm.com/download/win" -ForegroundColor Red
    }
}

# ============================================================
# [2/6] 安装 Python 3.12
# ============================================================
Log "[2/6] 检查 Python..."

$pyCmd = $null
foreach ($cmd in @("python", "python3", "py")) {
    if (Get-Command $cmd -ErrorAction SilentlyContinue) {
        $ver = & $cmd --version 2>&1
        if ($ver -match "Python 3\.(1[0-9]|[2-9]\d)") {
            $pyCmd = $cmd
            break
        }
    }
}

if ($pyCmd) {
    $pyVer = & $pyCmd --version
    Log "Python 已安装: $pyVer (命令: $pyCmd)"
} else {
    Log "正在安装 Python 3.12..."
    winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements --silent
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")

    if (Get-Command python -ErrorAction SilentlyContinue) {
        $pyCmd = "python"
        Log "Python 3.12 安装成功"
    } else {
        Write-Host "[错误] Python 安装失败，请手动安装: https://www.python.org/downloads/" -ForegroundColor Red
        Write-Host "  安装时务必勾选 'Add Python to PATH'" -ForegroundColor Yellow
    }
}

# ============================================================
# [3/6] 安装 Node.js (可选，用于 weixin-mcp)
# ============================================================
Log "[3/6] 检查 Node.js..."

if (Get-Command node -ErrorAction SilentlyContinue) {
    $nodeVer = node --version
    Log "Node.js 已安装: $nodeVer"
} else {
    Log "正在安装 Node.js..."
    winget install --id OpenJS.NodeJS.LTS -e --accept-source-agreements --accept-package-agreements --silent
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
    if (Get-Command node -ErrorAction SilentlyContinue) {
        $nodeVer = node --version
        Log "Node.js 安装成功: $nodeVer"
    } else {
        Write-Host "[警告] Node.js 安装失败，weixin-mcp 功能不可用（不影响自动回复核心功能）" -ForegroundColor Yellow
    }
}

# ============================================================
# [4/6] 创建目录结构 + 克隆代码
# ============================================================
Log "[4/6] 创建项目目录..."

if (-not (Test-Path $AI_ROOT)) {
    New-Item -ItemType Directory -Path $AI_ROOT -Force | Out-Null
    Log "创建 $AI_ROOT"
}
if (-not (Test-Path $WS_DIR)) {
    New-Item -ItemType Directory -Path $WS_DIR -Force | Out-Null
    Log "创建 $WS_DIR"
}

if (Test-Path "$CODE_DIR\.git") {
    Log "代码仓库已存在，执行 git pull..."
    Push-Location $CODE_DIR
    git pull origin main 2>&1
    Pop-Location
} elseif (Test-Path "$CODE_DIR\auto_reply.py") {
    Log "代码目录已存在但非 git 仓库，跳过克隆"
} else {
    Log "正在从 GitHub 克隆代码..."
    git clone $REPO_URL $CODE_DIR 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[警告] SSH 克隆失败，尝试 HTTPS..." -ForegroundColor Yellow
        git clone "https://github.com/kongbai1kongbai/AIChatDemo.git" $CODE_DIR 2>&1
    }
    if (Test-Path "$CODE_DIR\auto_reply.py") {
        Log "代码克隆成功"
    } else {
        Write-Host "[错误] 代码克隆失败，请手动克隆到 $CODE_DIR" -ForegroundColor Red
    }
}

# ============================================================
# [5/6] 安装 Python 依赖
# ============================================================
Log "[5/6] 安装 Python 依赖..."

if ($pyCmd) {
    Log "安装 wxautoz, requests, ddgs 和客服文件处理依赖..."
    & $pyCmd -m pip install --upgrade pip --quiet 2>&1
    & $pyCmd -m pip install wxautoz requests ddgs --quiet 2>&1
    $baseDepsExitCode = $LASTEXITCODE
    & $pyCmd -m pip install -r "$CODE_DIR\requirements-customer-service.txt" --quiet 2>&1
    $customerDepsExitCode = $LASTEXITCODE
    if ($baseDepsExitCode -eq 0 -and $customerDepsExitCode -eq 0) {
        Log "Python 依赖安装完成"
    } else {
        Write-Host "[警告] 部分依赖安装失败，请手动执行: $pyCmd -m pip install wxautoz requests ddgs; $pyCmd -m pip install -r `"$CODE_DIR\requirements-customer-service.txt`"" -ForegroundColor Yellow
    }
} else {
    Write-Host "[跳过] Python 未就绪，请稍后手动安装依赖" -ForegroundColor Yellow
}

# ============================================================
# [6/6] 生成 workspace 配置
# ============================================================
Log "[6/6] 生成 workspace 配置..."

# model_config.json
$modelConfig = @"
{
  "mode": "remote",
  "model": "gemini-3.5-flash",
  "api_base": "https://z.apiyihe.org/v1",
  "api_key": "YOUR_API_KEY_HERE",
  "remote_provider": "Gemini Flash (apiyihe)"
}
"@

$modelConfigPath = "$WS_DIR\model_config.json"
if (-not (Test-Path $modelConfigPath)) {
    Set-Content -Path $modelConfigPath -Value $modelConfig -Encoding UTF8
    Log "已创建 $modelConfigPath"
} else {
    Log "model_config.json 已存在，跳过"
}

# auto_reply_config.json
$replyConfigPath = "$WS_DIR\auto_reply_config.json"
if ((Test-Path "$CODE_DIR\auto_reply_config.json") -and -not (Test-Path $replyConfigPath)) {
    Copy-Item "$CODE_DIR\auto_reply_config.json" $replyConfigPath
    Log "已复制 auto_reply_config.json 到 workspace"
} elseif (Test-Path $replyConfigPath) {
    Log "auto_reply_config.json 已存在，跳过"
}

# ============================================================
# 安装 Open WebUI (可选)
# ============================================================
Write-Host ""
Write-Host "  是否安装 Open WebUI (Web 聊天界面)？" -ForegroundColor White
Write-Host "  需要 pip 安装，占用约 500MB 空间" -ForegroundColor Gray
Write-Host ""
$webuiChoice = Read-Host "  输入 Y 安装，其他跳过"

if ($webuiChoice -eq "Y" -or $webuiChoice -eq "y") {
    if ($pyCmd) {
        Log "安装 Open WebUI..."
        & $pyCmd -m pip install open-webui --quiet 2>&1
        if ($LASTEXITCODE -eq 0) {
            Log "Open WebUI 安装完成"
        } else {
            Write-Host "[警告] Open WebUI 安装失败" -ForegroundColor Yellow
        }
    }
}

# ============================================================
# 安装 Ollama (可选)
# ============================================================
Write-Host ""
Write-Host "  是否安装 Ollama (本地模型推理)？" -ForegroundColor White
Write-Host "  当前使用远程 API 模式，Ollama 为可选" -ForegroundColor Gray
Write-Host ""
$ollamaChoice = Read-Host "  输入 Y 安装，其他跳过"

if ($ollamaChoice -eq "Y" -or $ollamaChoice -eq "y") {
    Log "安装 Ollama..."
    winget install --id Ollama.Ollama -e --accept-source-agreements --accept-package-agreements --silent 2>&1
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
    if (Get-Command ollama -ErrorAction SilentlyContinue) {
        Log "Ollama 安装成功"
        Log "如需本地模型，执行: ollama pull qwen3:8b"
    } else {
        Write-Host "[警告] Ollama 安装失败，请手动安装: https://ollama.com" -ForegroundColor Yellow
    }
}

# ============================================================
# 汇总
# ============================================================
Write-Host ""
Write-Host "  ============================================" -ForegroundColor Green
Write-Host "    部署完成！" -ForegroundColor Green
Write-Host "  ============================================" -ForegroundColor Green
Write-Host ""
Write-Host "  目录结构:" -ForegroundColor White
Write-Host "    代码:   $CODE_DIR" -ForegroundColor Gray
Write-Host "    运行时: $WS_DIR" -ForegroundColor Gray
Write-Host ""
Write-Host "  下一步:" -ForegroundColor White
Write-Host "    1. 编辑 $WS_DIR\model_config.json，填入你的 API key" -ForegroundColor Yellow
Write-Host "    2. 启动微信 PC 版并登录" -ForegroundColor Yellow
Write-Host "    3. 双击 $CODE_DIR\auto-reply.bat 启动自动回复" -ForegroundColor Yellow
Write-Host ""
Write-Host "  常用命令:" -ForegroundColor White
Write-Host "    启动自动回复: cd $CODE_DIR && auto-reply.bat" -ForegroundColor Gray
Write-Host "    AI Station:   cd $CODE_DIR && start.bat" -ForegroundColor Gray
Write-Host ""

# 打开代码目录
explorer.exe $CODE_DIR
