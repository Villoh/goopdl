# Install goopdl from a GitHub release. Never edits your PATH.
#   irm https://villoh.github.io/goopdl/install.ps1 | iex
# Env: GOOPDL_VERSION (e.g. 1.3.1, default latest), GOOPDL_INSTALL_DIR (default %LOCALAPPDATA%\goopdl\bin)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'  # Invoke-WebRequest is very slow with the progress bar

$repo = 'Villoh/goopdl'
$dir = if ($env:GOOPDL_INSTALL_DIR) { $env:GOOPDL_INSTALL_DIR } else { Join-Path $env:LOCALAPPDATA 'goopdl\bin' }

$arch = switch ([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture) {
    'X64'   { 'x86_64' }
    'Arm64' { 'arm64' }
    default { throw "Unsupported architecture: $_" }
}
$asset = "goopdl-windows-$arch.exe"

$base = if ($env:GOOPDL_BASE_URL) { $env:GOOPDL_BASE_URL }
        elseif ($env:GOOPDL_VERSION) { "https://github.com/$repo/releases/download/v$($env:GOOPDL_VERSION.TrimStart('v'))" }
        else { "https://github.com/$repo/releases/latest/download" }

$tmp = Join-Path ([IO.Path]::GetTempPath()) ([IO.Path]::GetRandomFileName())
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
    Write-Host "Downloading $asset..."
    Invoke-WebRequest -UseBasicParsing "$base/$asset" -OutFile "$tmp\$asset"
    $sums = (Invoke-WebRequest -UseBasicParsing "$base/SHA256SUMS").Content

    $line = ($sums -split "`n") | Where-Object { $_.Trim() -match "\s$([regex]::Escape($asset))$" } | Select-Object -First 1
    if (-not $line) { throw "$asset not listed in SHA256SUMS" }
    $want = ($line.Trim() -split '\s+')[0]
    $got = (Get-FileHash "$tmp\$asset" -Algorithm SHA256).Hash
    if ($got -ne $want) { throw "Checksum mismatch for $asset" }

    New-Item -ItemType Directory -Path $dir -Force | Out-Null
    Move-Item "$tmp\$asset" "$dir\goopdl.exe" -Force
    Write-Host "Installed $(& "$dir\goopdl.exe" --version) to $dir\goopdl.exe"
}
finally {
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

if (($env:Path -split ';') -notcontains $dir) {
    Write-Host "Note: $dir is not in your PATH. Add it with:"
    Write-Host "  [Environment]::SetEnvironmentVariable('Path', `"`$env:Path;$dir`", 'User')"
}
