# Prepare a pinned llama.cpp runtime for a Windows x64 build.
param(
    [ValidateSet('cpu', 'vulkan')][string]$Backend = 'cpu',
    [string]$Destination
)

$ErrorActionPreference = 'Stop'
$version = 'b12493'
$archiveName = "llama-$version-bin-win-$Backend-x64.zip"
$release = 'https://github.com/ggml-org/llama.cpp/releases/download'
$checksums = @{
    cpu = '1f406c9d2d8f2a1ec995e02ac203873a062310c94dfd0f17034d62b49238c4ed'
    vulkan = '181796285119892857369c6a00d37d14d087ad4d280954b9921cd865ef1375ce'
}
if (-not $Destination) {
    $folder = if ($Backend -eq 'vulkan') { 'llama-runtime-vulkan' } else { 'llama-runtime' }
    $Destination = Join-Path (Split-Path $PSScriptRoot -Parent) $folder
}
$temp = Join-Path ([System.IO.Path]::GetTempPath()) ([Guid]::NewGuid().ToString())
New-Item -Path $temp -ItemType Directory | Out-Null

try {
    $archive = Join-Path $temp $archiveName
    Invoke-WebRequest -UseBasicParsing -Uri "$release/$version/$archiveName" -OutFile $archive
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash -ne $checksums[$Backend]) {
        throw 'llama.cpp runtime archive failed SHA-256 verification.'
    }
    $expanded = Join-Path $temp 'expanded'
    Expand-Archive -LiteralPath $archive -DestinationPath $expanded
    $bundle = (Get-ChildItem $expanded -Recurse -File -Filter 'llama-completion.exe' | Select-Object -First 1).DirectoryName
    if (-not $bundle) { throw 'llama-completion.exe not found in the verified runtime archive.' }
    # Copy the runtime and companion DLLs together, omitting other tools and servers.
    $output = New-Item -Path $Destination -ItemType Directory -Force
    $patterns = @('llama-completion.exe', 'llama-completion-impl.dll', 'llama.dll', 'llama-common.dll', 'ggml*.dll', 'libomp.dll', 'LICENSE*')
    foreach ($pattern in $patterns) {
        Get-ChildItem -Path $bundle -Filter $pattern -File | Copy-Item -Destination $output.FullName -Force
    }
    if ($Backend -eq 'vulkan' -and -not (Test-Path (Join-Path $output.FullName 'ggml-vulkan.dll'))) {
        throw 'Vulkan backend DLL is missing from the runtime.'
    }
    $license = Join-Path $output.FullName 'LICENSE-llama.cpp'
    Invoke-WebRequest -UseBasicParsing -Uri "https://raw.githubusercontent.com/ggml-org/llama.cpp/$version/LICENSE" -OutFile $license
    $launcher = Join-Path $output.FullName 'llama-completion.exe'
    & $launcher --version
    if ($LASTEXITCODE -ne 0) { throw "llama.cpp failed to start (exit $LASTEXITCODE)." }
} finally {
    Remove-Item -LiteralPath $temp -Recurse -Force
}
