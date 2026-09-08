$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Find-RealBinary($name) {
    if ((Test-Path ".\$name") -and ((Get-Item ".\$name").Length -gt 10MB)) {
        return (Get-Item ".\$name").FullName
    }
    # Check chocolatey actual library directory (not the bin shim)
    if (Test-Path "C:\ProgramData\chocolatey\lib\ffmpeg") {
        $c = Get-ChildItem -Path "C:\ProgramData\chocolatey\lib\ffmpeg" -Filter $name -Recurse -ErrorAction SilentlyContinue | Where-Object { $_.Length -gt 10MB } | Select-Object -First 1
        if ($c) { return $c.FullName }
    }
    # Check fallback paths
    $fallbackPaths = @(
        "C:\Users\niral\Downloads\installer_files\ffmpeg\bin",
        "C:\ffmpeg\bin",
        "$env:LOCALAPPDATA\Microsoft\WinGet\Packages"
    )
    foreach ($p in $fallbackPaths) {
        if (Test-Path $p) {
            $c = Get-ChildItem -Path $p -Filter $name -Recurse -ErrorAction SilentlyContinue | Where-Object { $_.Length -gt 10MB } | Select-Object -First 1
            if ($c) { return $c.FullName }
        }
    }
    # Check PATH commands that are real binaries
    $cmds = Get-Command $name -All -ErrorAction SilentlyContinue
    foreach ($cmd in $cmds) {
        if ($cmd.Source -and (Test-Path $cmd.Source) -and ((Get-Item $cmd.Source).Length -gt 10MB)) {
            return $cmd.Source
        }
    }
    return $null
}

$realFfmpeg = Find-RealBinary "ffmpeg.exe"
$realFfprobe = Find-RealBinary "ffprobe.exe"

if (-not $realFfmpeg -or -not $realFfprobe) {
    throw "Real FFmpeg/FFprobe binaries (>10MB) could not be located. Ensure genuine binaries are available before building."
}

if ((Get-Item ".\ffmpeg.exe" -ErrorAction SilentlyContinue).FullName -ne (Get-Item $realFfmpeg).FullName) {
    Copy-Item $realFfmpeg ".\ffmpeg.exe" -Force
}
if ((Get-Item ".\ffprobe.exe" -ErrorAction SilentlyContinue).FullName -ne (Get-Item $realFfprobe).FullName) {
    Copy-Item $realFfprobe ".\ffprobe.exe" -Force
}

$fSize = (Get-Item ".\ffmpeg.exe").Length
$pSize = (Get-Item ".\ffprobe.exe").Length
if ($fSize -lt 10MB -or $pSize -lt 10MB) {
    throw "ffmpeg.exe ($fSize bytes) or ffprobe.exe ($pSize bytes) is smaller than 10MB! Shims/stubs cannot be bundled."
}
Write-Host "Verified genuine FFmpeg ($fSize bytes) and FFprobe ($pSize bytes)."

python -m pip install -r requirements.txt
python -m PyInstaller --noconfirm --clean simple_audio_clipper.spec

$exe = Join-Path $PSScriptRoot "dist\Ultimate Audio Studio.exe"
if (Test-Path $exe) {
    Write-Host "Build succeeded: $exe"
} else {
    throw "Build finished but Ultimate Audio Studio.exe was not created."
}
