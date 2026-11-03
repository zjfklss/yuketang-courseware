# 构建 Windows 版：建虚拟环境 → 装依赖 → 跑测试 → 生成 ico → PyInstaller
#
# 用法（在仓库根目录或任意位置都行）：
#   powershell -ExecutionPolicy Bypass -File packaging\windows\build-windows.ps1
#
# 可选参数：
#   -Python <路径>   用现成的解释器（默认用 .venv\Scripts\python.exe）
#   -SkipTests       跳过测试
#   -SkipDeps        跳过 pip 安装（依赖已经装好时）
#
# 产物：
#   dist\yuketang-courseware\yuketang-courseware.exe      图形界面（双击）
#   dist\yuketang-courseware\yuketang-courseware-cli.exe  命令行（带控制台）

param(
    [string]$Python = "",
    [switch]$SkipTests,
    [switch]$SkipDeps
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Push-Location $root
try {
    if (-not $Python) {
        $Python = Join-Path $root ".venv\Scripts\python.exe"
    }

    if (-not (Test-Path $Python)) {
        if ($SkipDeps) {
            throw "找不到 Python 解释器：$Python（去掉 -SkipDeps 让它自动建 venv）"
        }
        Write-Host "==> 创建虚拟环境 .venv"
        $bootstrap = (Get-Command python -ErrorAction SilentlyContinue)
        if (-not $bootstrap) {
            throw "PATH 里没有 python。先装 Python 3.9+，安装时勾上 Add Python to PATH。"
        }
        & python -m venv (Join-Path $root ".venv")
        $Python = Join-Path $root ".venv\Scripts\python.exe"
        & $Python -m pip install --upgrade pip
    }

    if (-not $SkipDeps) {
        Write-Host "==> 安装依赖"
        & $Python -m pip install -r (Join-Path $root "requirements-windows.txt")
        if ($LASTEXITCODE -ne 0) { throw "依赖安装失败" }
    }

    if (-not $SkipTests) {
        Write-Host "==> 跑测试"
        & $Python -m unittest discover -s tests
        if ($LASTEXITCODE -ne 0) { throw "测试没通过，先修好再打包" }
    }

    Write-Host "==> 生成图标"
    & $Python (Join-Path $root "packaging\windows\make-ico.py")
    if ($LASTEXITCODE -ne 0) { throw "图标生成失败" }

    Write-Host "==> PyInstaller 打包"
    & $Python -m PyInstaller --noconfirm --clean (Join-Path $root "packaging\windows\yuketang-courseware.spec")
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败" }

    $exe = Join-Path $root "dist\yuketang-courseware\yuketang-courseware.exe"
    Write-Host ""
    Write-Host "完成：$exe"
    Write-Host "命令行：dist\yuketang-courseware\yuketang-courseware-cli.exe --help"
} finally {
    Pop-Location
}
