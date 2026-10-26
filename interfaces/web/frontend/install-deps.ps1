#requires -Version 7.0
[CmdletBinding()]
param(
    [string]$DependenciesDirectory = 'D:\nvm\project-deps\agent-harness',
    [switch]$Refresh
)

$ErrorActionPreference = 'Stop'
$comparison = [System.StringComparison]::OrdinalIgnoreCase
$frontendRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$workspaceRoot = [System.IO.Path]::GetFullPath((Join-Path $frontendRoot '..\..\..')).TrimEnd('\')
$localModules = Join-Path $frontendRoot 'node_modules'
$locationChanged = $false

function Get-OptionalItem {
    param([string]$Path)
    try { return Get-Item -LiteralPath $Path -Force -ErrorAction Stop }
    catch [System.Management.Automation.ItemNotFoundException] { return $null }
}

function Test-Inside {
    param([string]$Path, [string]$Root)
    return $Path.StartsWith($Root.TrimEnd('\') + '\', $comparison)
}

function Assert-NoReparseAncestor {
    param([string]$Path)
    $ancestor = $Path
    while ($ancestor) {
        $item = Get-OptionalItem $ancestor
        if ($item -and (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)) {
            throw "A path contains a link/reparse point: $ancestor"
        }
        $parent = Split-Path -Parent $ancestor
        if ($parent -eq $ancestor) { break }
        $ancestor = $parent
    }
}

function Assert-ExpectedJunction {
    param([System.IO.FileSystemInfo]$Item)
    if ($Item.LinkType -ne 'Junction' -or @($Item.Target).Count -ne 1) {
        throw "Expected a directory junction at $localModules; refusing to replace another link."
    }
    $actualTarget = [System.IO.Path]::GetFullPath([string]@($Item.Target)[0]).TrimEnd('\')
    if (-not $actualTarget.Equals($externalModules, $comparison)) {
        throw "Existing junction targets '$actualTarget', not '$externalModules'. No files were moved."
    }
}

function Test-SameFile {
    param([string]$First, [string]$Second)
    if (-not (Test-Path -LiteralPath $Second -PathType Leaf)) { return $false }
    return (Get-FileHash -LiteralPath $First -Algorithm SHA256).Hash -eq
        (Get-FileHash -LiteralPath $Second -Algorithm SHA256).Hash
}

function Test-InstalledLock {
    param([string]$ModulesPath)
    $hiddenLockPath = Join-Path $ModulesPath '.package-lock.json'
    if (-not (Test-Path -LiteralPath $hiddenLockPath -PathType Leaf)) { return $false }
    try {
        $installed = Get-Content -LiteralPath $hiddenLockPath -Raw | ConvertFrom-Json -AsHashtable
        if ($installed.name -ne $manifest.name -or -not $installed.packages) { return $false }
        foreach ($entry in $installed.packages.GetEnumerator()) {
            $expected = $lock.packages[$entry.Key]
            if (-not $expected -or $expected.version -ne $entry.Value.version) { return $false }
        }
        foreach ($group in @('dependencies', 'devDependencies')) {
            if (-not $manifest.$group) { continue }
            foreach ($dependency in $manifest.$group.PSObject.Properties) {
                $key = 'node_modules/' + $dependency.Name
                if (-not $installed.packages[$key] -or
                    -not (Test-Path -LiteralPath (Join-Path $ModulesPath ($dependency.Name + '/package.json')) -PathType Leaf)) {
                    return $false
                }
            }
        }
        return $true
    }
    catch { return $false }
}

try {
    if (-not (Test-Inside $localModules $workspaceRoot)) {
        throw "The frontend node_modules path must be inside this workspace: $localModules"
    }
    Assert-NoReparseAncestor $frontendRoot
    if ($DependenciesDirectory -notmatch '^[A-Za-z]:[\\/]') {
        throw 'DependenciesDirectory must be an absolute local drive path.'
    }
    $externalRoot = [System.IO.Path]::GetFullPath($DependenciesDirectory).TrimEnd('\')
    $externalModules = Join-Path $externalRoot 'node_modules'
    $driveRoot = [System.IO.Path]::GetPathRoot($externalRoot)
    if ($externalRoot.Equals($driveRoot.TrimEnd('\'), $comparison) -or
        $externalRoot.Equals($workspaceRoot, $comparison) -or
        (Test-Inside $externalRoot $workspaceRoot) -or (Test-Inside $workspaceRoot $externalRoot)) {
        throw 'Use a dedicated project dependency directory outside the workspace, not a drive root or workspace ancestor.'
    }
    # Do not turn a Node/npm/nvm installation root into an npm project.
    $protectedRoots = @('D:\nodejs', 'D:\nvm', $env:NVM_HOME, $env:NVM_SYMLINK)
    $nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
    if ($nodeCommand) { $protectedRoots += Split-Path -Parent $nodeCommand.Source }
    foreach ($protectedRoot in $protectedRoots) {
        if ($protectedRoot -and $externalRoot.Equals([System.IO.Path]::GetFullPath($protectedRoot).TrimEnd('\'), $comparison)) {
            throw "Use a project-specific child directory, not a tool installation root: $externalRoot"
        }
    }
    Assert-NoReparseAncestor $externalModules

    $sourceManifest = Join-Path $frontendRoot 'package.json'
    $sourceLock = Join-Path $frontendRoot 'package-lock.json'
    $manifest = Get-Content -LiteralPath $sourceManifest -Raw | ConvertFrom-Json
    # npm's root package is stored under an empty key, which requires hashtables.
    $lock = Get-Content -LiteralPath $sourceLock -Raw | ConvertFrom-Json -AsHashtable
    if (-not $manifest.name -or $lock.name -ne $manifest.name -or -not $lock.packages) {
        throw 'The source package.json and package-lock.json must describe the same npm project.'
    }
    if ($manifest.workspaces) { throw 'This helper does not support npm workspaces.' }
    foreach ($group in @('dependencies', 'devDependencies', 'optionalDependencies')) {
        if (-not $manifest.$group) { continue }
        foreach ($dependency in $manifest.$group.PSObject.Properties) {
            if ([string]$dependency.Value -match '^(file:|link:|workspace:|\.{1,2}[\\/]|[A-Za-z]:[\\/])') {
                throw "Local-path dependencies require a different layout: $($dependency.Name)"
            }
        }
    }

    $externalManifest = Join-Path $externalRoot 'package.json'
    $externalLock = Join-Path $externalRoot 'package-lock.json'
    Assert-NoReparseAncestor $externalManifest
    Assert-NoReparseAncestor $externalLock
    $externalRootItem = Get-OptionalItem $externalRoot
    if ($externalRootItem -and -not $externalRootItem.PSIsContainer) { throw "Not a directory: $externalRoot" }
    if (Test-Path -LiteralPath $externalManifest -PathType Leaf) {
        $savedManifest = Get-Content -LiteralPath $externalManifest -Raw | ConvertFrom-Json
        if ($savedManifest.name -ne $manifest.name) {
            throw "The destination belongs to another npm project: $externalRoot"
        }
    }
    elseif ($externalRootItem -and @(Get-ChildItem -LiteralPath $externalRoot -Force).Count -gt 0) {
        throw "The destination is nonempty and has no matching project manifest: $externalRoot"
    }

    $localItem = Get-OptionalItem $localModules
    $externalItem = Get-OptionalItem $externalModules
    if ($externalItem -and -not $externalItem.PSIsContainer) { throw "Not a directory: $externalModules" }
    $localIsJunction = $false
    if ($localItem) {
        if (($localItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            Assert-ExpectedJunction $localItem
            $localIsJunction = $true
        }
        elseif (-not $localItem.PSIsContainer) { throw "Not a directory: $localModules" }
        elseif ($externalItem) {
            throw 'Both local and external node_modules directories exist. Refusing to merge or overwrite them.'
        }
    }

    $savedFilesMatch = (Test-SameFile $sourceManifest $externalManifest) -and
        (Test-SameFile $sourceLock $externalLock)
    $needsInstall = $Refresh -or -not $externalItem -or -not $savedFilesMatch -or
        -not (Test-InstalledLock $externalModules)
    if ($localItem -and -not $localIsJunction) {
        $needsInstall = $Refresh -or -not (Test-InstalledLock $localModules)
    }

    $null = New-Item -ItemType Directory -Path $externalRoot -Force
    Copy-Item -LiteralPath $sourceManifest -Destination $externalManifest -Force
    Copy-Item -LiteralPath $sourceLock -Destination $externalLock -Force

    if ($localItem -and -not $localIsJunction) {
        # Verify both absolute boundaries immediately before the only directory move.
        Assert-NoReparseAncestor $localModules
        Assert-NoReparseAncestor $externalModules
        if (-not (Test-Inside $localModules $workspaceRoot) -or
            -not (Test-Inside $externalModules $externalRoot)) { throw 'Directory move boundary validation failed.' }
        Move-Item -LiteralPath $localModules -Destination $externalModules
        try {
            $null = New-Item -ItemType Junction -Path $localModules -Target $externalModules
            $localIsJunction = $true
        }
        catch {
            $creationError = $_
            # A failed junction creation normally leaves no entry; preserve any unexpected entry.
            if (-not (Get-OptionalItem $localModules)) {
                Assert-NoReparseAncestor $externalModules
                Move-Item -LiteralPath $externalModules -Destination $localModules
            }
            throw $creationError
        }
        Write-Host "Moved project dependencies to: $externalModules"
    }

    if ($needsInstall) {
        $npm = Get-Command npm.cmd -ErrorAction Stop
        Assert-NoReparseAncestor $externalModules
        # A failed npm ci must not be mistaken for a completed installation on the next run.
        $hiddenLock = Join-Path $externalModules '.package-lock.json'
        if (Test-Path -LiteralPath $hiddenLock -PathType Leaf) {
            Remove-Item -LiteralPath $hiddenLock -Force
        }
        Push-Location -LiteralPath $externalRoot
        $locationChanged = $true
        & $npm.Source ci --include=dev --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { throw "npm ci failed with exit code $LASTEXITCODE in $externalRoot. Run this helper again to retry." }
        if (-not (Test-InstalledLock $externalModules)) { throw 'npm ci finished without a complete dependency installation.' }
    }
    else { Write-Host 'Reusing dependencies that match package.json and package-lock.json.' }

    if (-not $localIsJunction) {
        $null = New-Item -ItemType Junction -Path $localModules -Target $externalModules
    }
    Assert-ExpectedJunction (Get-Item -LiteralPath $localModules -Force)
    Write-Host "Frontend node_modules is a junction: $localModules -> $externalModules"
    Write-Host 'Use this helper for dependency installation. Do not run npm install/npm ci in the source frontend directory.'
}
finally {
    if ($locationChanged) { Pop-Location }
}
