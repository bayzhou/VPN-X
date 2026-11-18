[CmdletBinding()]
param(
    [string]$Python = 'python',
    [string]$Installer = '',
    [string]$OutputDirectory = '',
    [string]$SevenZip = '',
    [string]$CatalogSource = '',
    [switch]$MaskCatalogSource
)

$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
    $bundleDir = Join-Path $PSScriptRoot 'installers'
    $installerPattern = '^Clash[. ]Verge_(\d+\.\d+\.\d+)_x64-setup\.exe$'
    $defaultName = 'Clash.Verge_2.5.10_x64-setup.exe'
    if (-not $Installer) {
        $bundled = Get-ChildItem -LiteralPath $bundleDir -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match $installerPattern } |
            Sort-Object { [version]([regex]::Match($_.Name, $installerPattern).Groups[1].Value) } -Descending |
            Select-Object -First 1
        if ($bundled) {
            $Installer = $bundled.FullName
        } else {
            $Installer = Join-Path (Split-Path -Parent $PSScriptRoot) $defaultName
        }
    }
    $package = Get-Item -LiteralPath $Installer
    if ($package.Name -notmatch $installerPattern) {
        throw 'Expected a numbered Clash Verge x64 NSIS installer.'
    }
    $expectedHashes = @{
        '2.4.7' = '6642881B212BCD4766E81D56AEC7497063BA5198C287F7D00A2ECF13B6F8B61A'
        '2.5.10' = '929DA9303B643B80CE309AB29F618E37A5562F515D1FA55EBF5B86C822504450'
    }
    $version = [regex]::Match($package.Name, $installerPattern).Groups[1].Value
    if ($expectedHashes.ContainsKey($version) -and
        (Get-FileHash -LiteralPath $package.FullName -Algorithm SHA256).Hash -ne $expectedHashes[$version]) {
        throw "Clash Verge $version installer does not match the official SHA-256."
    }
    if (-not $SevenZip) {
        $SevenZip = (Get-Command 7z.exe -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1).Source
        if (-not $SevenZip) {
            $SevenZip = Join-Path $env:ProgramFiles '7-Zip\7z.exe'
        }
    }
    if (-not (Test-Path -LiteralPath $SevenZip -PathType Leaf)) {
        throw '7-Zip is needed to unpack the official installer. Pass -SevenZip with the path to 7z.exe.'
    }
    $SevenZip = (Resolve-Path -LiteralPath $SevenZip).ProviderPath
    $null = New-Item -ItemType Directory -Path $bundleDir -Force
    $destination = Join-Path $bundleDir $package.Name
    if ($package.FullName -ne $destination) {
        Copy-Item -LiteralPath $package.FullName -Destination $destination
    }
    $pythonExe = (Get-Command $Python -CommandType Application | Select-Object -First 1).Source
    $appVersion = & $pythonExe -c 'from app_info import APP_VERSION; print(APP_VERSION)'
    $releaseName = & $pythonExe -c 'from app_info import RELEASE_NAME; print(RELEASE_NAME)'
    if ($appVersion -notmatch '^\d+\.\d+$' -or $releaseName -ne "VPN-X-V$appVersion") {
        throw 'Invalid VPN-X application version.'
    }
    if (-not $OutputDirectory) {
        $OutputDirectory = if ($MaskCatalogSource) { 'dist\masked' } else { 'dist\unmasked' }
    }
    $outputRoot = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputDirectory)
    $logo = (Get-Item -LiteralPath 'logo.ico').FullName
    if ($MaskCatalogSource -and $CatalogSource) {
        throw 'Use either -MaskCatalogSource or -CatalogSource.'
    }
    if (-not $MaskCatalogSource) {
        if (-not $CatalogSource) {
            $CatalogSource = Join-Path $PSScriptRoot 'catalog-source.json'
        }
        $CatalogSource = (Get-Item -LiteralPath $CatalogSource).FullName
        & $pythonExe -c 'import sys; from catalog import CatalogSource; sys.exit(0 if CatalogSource.load(sys.argv[1]).configured else 1)' $CatalogSource
        if ($LASTEXITCODE) {
            throw 'The source JSON must contain a valid VXP URL and key.'
        }
    }
    $versionTuple = $appVersion.Replace('.', ',') + ',0,0'
    $versionResource = [IO.Path]::GetTempFileName()
    $versionInfo = @"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($versionTuple), prodvers=($versionTuple), mask=0x3f, flags=0,
                    OS=0x40004, fileType=1, subtype=0, date=(0,0)),
  kids=[StringFileInfo([StringTable('040904B0', [
      StringStruct('FileDescription', 'VPN-X'),
      StringStruct('FileVersion', '$appVersion'),
      StringStruct('ProductName', 'VPN-X'),
      StringStruct('ProductVersion', '$appVersion'),
      StringStruct('OriginalFilename', '$releaseName.exe')
    ])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])]
)
"@
    $buildArgs = @('--noconfirm', '--clean', '--onedir', '--windowed',
                   '--name', $releaseName, '--distpath', $outputRoot, '--version-file', $versionResource,
                   '--icon', $logo, '--add-data', "$logo;.",
                   '--add-data', "$destination;installers", 'vpn_x.py')
    $previousPath = $env:Path
    try {
        [IO.File]::WriteAllText($versionResource, $versionInfo)
        # An unrelated ICU on PATH can shadow Windows ICU during DLL collection.
        $env:Path = "$env:SystemRoot\System32;$env:SystemRoot;$(Split-Path -Parent $pythonExe)"
        & $pythonExe -m PyInstaller @buildArgs
        if ($LASTEXITCODE) {
            throw "PyInstaller failed ($LASTEXITCODE)."
        }

        $release = Join-Path $outputRoot $releaseName
        $shadowDll = Get-ChildItem -LiteralPath $release -Recurse -File |
            Where-Object { $_.Name -in 'icu.dll', 'icuuc.dll', 'icuin.dll' } |
            Select-Object -First 1
        if ($shadowDll) {
            throw "A bundled DLL shadows Windows ICU: $($shadowDll.FullName)"
        }
        $clash = Join-Path $release 'clash'
        & $SevenZip x -y $destination "-o$clash" 'clash-verge.exe' 'verge-mihomo*.exe' 'resources\*'
        if ($LASTEXITCODE) {
            throw 'Could not unpack the Clash Verge runtime into the release.'
        }
        $sourceTarget = Join-Path $release 'catalog-source.json'
        if ($MaskCatalogSource) {
            & $pythonExe -c 'import sys; from catalog import CatalogSource; CatalogSource().save(sys.argv[1])' $sourceTarget
        } else {
            & $pythonExe -c 'import sys; from catalog import CatalogSource; CatalogSource.load(sys.argv[1]).save(sys.argv[2])' $CatalogSource $sourceTarget
        }
        if ($LASTEXITCODE) {
            throw 'Could not prepare catalog-source.json.'
        }
        $exe = Join-Path $release "$releaseName.exe"
        & $pythonExe -c 'import subprocess, sys; [subprocess.run([sys.argv[1], check], timeout=20, check=True) for check in ("--check-startup", "--check-clash")]' $exe
        if ($LASTEXITCODE) {
            throw 'The packaged VPN-X executable or its local Clash runtime did not pass the startup check.'
        }
        Copy-Item -LiteralPath 'README.md' -Destination (Join-Path $release 'README.md') -Force
        $zipScript = @'
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
import sys

root = Path(sys.argv[1])
target = root.parent / (root.name + ".zip")
temporary = target.with_suffix(".zip.tmp")
try:
    with ZipFile(temporary, "w", ZIP_DEFLATED, compresslevel=4) as bundle:
        for file in sorted(root.rglob("*")):
            if file.is_file():
                bundle.write(file, file.relative_to(root.parent))
    with ZipFile(temporary) as bundle:
        if bundle.testzip() is not None:
            raise RuntimeError("Archive integrity check failed.")
    temporary.replace(target)
finally:
    temporary.unlink(missing_ok=True)
print(target)
'@
        & $pythonExe -c $zipScript $release
        if ($LASTEXITCODE) {
            throw 'Could not create the release ZIP.'
        }
    }
    finally {
        $env:Path = $previousPath
        Remove-Item -LiteralPath $versionResource -ErrorAction SilentlyContinue
    }
}
finally {
    Pop-Location
}
