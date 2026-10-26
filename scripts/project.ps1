[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('run', 'dev', 'test', 'check', 'build', 'deps', 'clean')]
    [string]$Action = 'run'
)

$ErrorActionPreference = 'Stop'
$projectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$projectPrefix = $projectRoot.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$exitCode = 0
$locationChanged = $false
$savedEnvironment = @{}

function Invoke-ProjectCommand {
    param([string]$Executable, [string[]]$CommandArguments)

    & $Executable @CommandArguments
    if ($LASTEXITCODE -ne 0) {
        throw "命令执行失败（退出码 $LASTEXITCODE）：$Executable $($CommandArguments -join ' ')"
    }
}

function Assert-ProjectPath {
    param([string]$Path)

    $absolutePath = [System.IO.Path]::GetFullPath($Path)
    if (-not $absolutePath.StartsWith($projectPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝清理项目目录以外的路径：$absolutePath"
    }

    # 同时检查父路径，防止通过 frontend 等目录的链接间接越界。
    $ancestor = $absolutePath
    while ($ancestor.Length -ge $projectRoot.Length) {
        try {
            $item = Get-Item -LiteralPath $ancestor -Force
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "拒绝清理链接或重解析点：$ancestor"
            }
        }
        catch [System.Management.Automation.ItemNotFoundException] {
            # 尚未生成的缓存目录无需清理；现有父目录仍须检查。
        }
        if ($ancestor.Equals($projectRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            break
        }
        $ancestor = Split-Path -Parent $ancestor
    }
    return $absolutePath
}

function Assert-NoReparseTree {
    param([System.IO.FileSystemInfo]$RootItem)

    $pending = New-Object 'System.Collections.Generic.Stack[System.IO.FileSystemInfo]'
    $pending.Push($RootItem)
    while ($pending.Count -gt 0) {
        $item = $pending.Pop()
        $null = Assert-ProjectPath $item.FullName
        if ($item.PSIsContainer) {
            foreach ($child in Get-ChildItem -LiteralPath $item.FullName -Force) {
                $pending.Push($child)
            }
        }
    }
}

function Clear-ProjectArtifacts {
    $candidatePaths = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
    $fixedPaths = @(
        '.artifacts', '.test-artifacts', '.pytest_cache', '.ruff_cache',
        'build', 'dist', 'agent_harness.egg-info', '__pycache__',
        'interfaces\web\frontend\.next', 'interfaces\web\frontend\tsconfig.tsbuildinfo'
    )
    foreach ($relativePath in $fixedPaths) {
        $null = $candidatePaths.Add((Assert-ProjectPath (Join-Path $projectRoot $relativePath)))
    }
    foreach ($item in Get-ChildItem -LiteralPath $projectRoot -Force -Filter '.test-tmp*') {
        $null = $candidatePaths.Add((Assert-ProjectPath $item.FullName))
    }

    # 只遍历手写源码目录，不扫描 .venv、node_modules 或其他依赖。
    foreach ($sourceDirectory in @('application', 'core', 'infrastructure', 'interfaces', 'services', 'tools', 'tests', 'skills')) {
        $sourcePath = Assert-ProjectPath (Join-Path $projectRoot $sourceDirectory)
        if (-not (Test-Path -LiteralPath $sourcePath -PathType Container)) {
            continue
        }
        $pending = New-Object 'System.Collections.Generic.Stack[string]'
        $pending.Push($sourcePath)
        while ($pending.Count -gt 0) {
            $directory = $pending.Pop()
            foreach ($child in Get-ChildItem -LiteralPath $directory -Force) {
                if ($child.PSIsContainer -and $child.Name -eq 'frontend') {
                    continue
                }
                $childPath = Assert-ProjectPath $child.FullName
                if ($child.PSIsContainer) {
                    if ($child.Name -eq '__pycache__') {
                        $null = $candidatePaths.Add($childPath)
                    }
                    else {
                        $pending.Push($childPath)
                    }
                }
            }
        }
    }

    $gitRootOutput = @(& git -C $projectRoot rev-parse --show-toplevel)
    if ($LASTEXITCODE -ne 0 -or $gitRootOutput.Count -ne 1) {
        throw '无法确认 Git 仓库根目录，已取消清理。'
    }
    $gitRoot = [System.IO.Path]::GetFullPath($gitRootOutput[0])
    if (-not $gitRoot.Equals($projectRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw '当前目录不是 Git 仓库根目录，已取消清理。'
    }
    $trackedOutput = @(& git -C $projectRoot ls-files --cached -z)
    if ($LASTEXITCODE -ne 0) {
        throw '无法读取 Git 跟踪文件，已取消清理。'
    }
    $trackedPaths = @(
        foreach ($relativePath in (($trackedOutput -join "`n") -split "`0")) {
            if ($relativePath) {
                [System.IO.Path]::GetFullPath((Join-Path $projectRoot $relativePath))
            }
        }
    )

    # 所有路径检查通过后才开始删除，任何链接或源码冲突都会整体中止。
    $candidates = @(
        foreach ($candidatePath in $candidatePaths) {
            try {
                $candidate = Get-Item -LiteralPath $candidatePath -Force
            }
            catch [System.Management.Automation.ItemNotFoundException] {
                continue
            }
            Assert-NoReparseTree $candidate
            $candidatePrefix = $candidatePath.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
            foreach ($trackedPath in $trackedPaths) {
                if ($trackedPath.Equals($candidatePath, [System.StringComparison]::OrdinalIgnoreCase) -or
                    $trackedPath.StartsWith($candidatePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
                    throw "清理路径包含 Git 跟踪文件，已取消清理：$trackedPath"
                }
            }
            $candidate
        }
    )
    foreach ($candidate in $candidates) {
        $absolutePath = Assert-ProjectPath $candidate.FullName
        Remove-Item -LiteralPath $absolutePath -Recurse -Force
        Write-Host "已清理：$($absolutePath.Substring($projectPrefix.Length))"
    }
    if ($candidates.Count -eq 0) {
        Write-Host '没有需要清理的过程文件。'
    }
}

try {
    foreach ($variableName in @('PYTHONDONTWRITEBYTECODE', 'NEXT_TELEMETRY_DISABLED')) {
        $savedEnvironment[$variableName] = [System.Environment]::GetEnvironmentVariable($variableName, 'Process')
        [System.Environment]::SetEnvironmentVariable($variableName, '1', 'Process')
    }
    Push-Location -LiteralPath $projectRoot
    $locationChanged = $true

    if ($Action -in @('run', 'dev', 'test', 'check') -and -not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw '缺少 .venv\Scripts\python.exe，请先按 README 安装项目依赖。'
    }
    if ($Action -in @('check', 'build')) {
        $npm = (Get-Command npm.cmd).Source
    }

    switch ($Action) {
        'run' { Invoke-ProjectCommand $python @('-m', 'agent_harness') }
        'dev' { Invoke-ProjectCommand $python @('-m', 'agent_harness', '--dev') }
        'test' { Invoke-ProjectCommand $python @('-m', 'pytest', 'tests', '-q') }
        'check' {
            Invoke-ProjectCommand $python @(
                '-m', 'ruff', 'check', 'application', 'core', 'infrastructure',
                'interfaces', 'services', 'tools', 'tests', 'config.py',
                '__init__.py', '__main__.py', '--select', 'F'
            )
            Invoke-ProjectCommand $npm @('--prefix', 'interfaces/web/frontend', 'run', 'typecheck')
        }
        'build' { Invoke-ProjectCommand $npm @('--prefix', 'interfaces/web/frontend', 'run', 'build') }
        'deps' { & (Join-Path $projectRoot 'interfaces\web\frontend\install-deps.ps1') }
        'clean' { Clear-ProjectArtifacts }
    }
}
catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    $exitCode = 1
}
finally {
    if ($locationChanged) {
        Pop-Location
    }
    foreach ($variableName in $savedEnvironment.Keys) {
        [System.Environment]::SetEnvironmentVariable($variableName, $savedEnvironment[$variableName], 'Process')
    }
}
exit $exitCode
