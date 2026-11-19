# Prepare a pinned llama.cpp runtime for a Windows x64 build.
param(
    [ValidateSet('cpu', 'vulkan', 'cuda')][string]$Backend = 'cpu',
    [string]$Destination
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$version = 'b12493'
$assetBackend = if ($Backend -eq 'cuda') { 'cuda-12.4' } else { $Backend }
$archiveName = "llama-$version-bin-win-$assetBackend-x64.zip"
$release = 'https://github.com/ggml-org/llama.cpp/releases/download'
$checksums = @{
    cpu = '1f406c9d2d8f2a1ec995e02ac203873a062310c94dfd0f17034d62b49238c4ed'
    vulkan = '181796285119892857369c6a00d37d14d087ad4d280954b9921cd865ef1375ce'
    cuda = 'c0343480a38a4e64e02b931f4f49c66219bc7291db36e86bdceaa6d8849a6c42'
}
if (-not $Destination) {
    $folder = if ($Backend -eq 'cpu') { 'llama-runtime' } else { "llama-runtime-$Backend" }
    $Destination = Join-Path (Split-Path $PSScriptRoot -Parent) $folder
}
$temp = Join-Path ([System.IO.Path]::GetTempPath()) ([Guid]::NewGuid().ToString())
New-Item -Path $temp -ItemType Directory | Out-Null

function Expand-VerifiedArchive($Name, $Checksum, $Directory) {
    $archive = Join-Path $temp $Name
    Invoke-WebRequest -UseBasicParsing -Uri "$release/$version/$Name" -OutFile $archive
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash -ne $Checksum) {
        throw "$Name failed SHA-256 verification."
    }
    Expand-Archive -LiteralPath $archive -DestinationPath $Directory
}

try {
    $expanded = Join-Path $temp 'expanded'
    Expand-VerifiedArchive $archiveName $checksums[$Backend] $expanded
    $bundle = (Get-ChildItem $expanded -Recurse -File -Filter 'llama-completion.exe' | Select-Object -First 1).DirectoryName
    if (-not $bundle) { throw 'llama-completion.exe not found in the verified runtime archive.' }
    # Copy the runtime and companion DLLs together, omitting other tools and servers.
    $output = New-Item -Path $Destination -ItemType Directory -Force
    $patterns = @('llama-completion.exe', 'llama-completion-impl.dll', 'llama.dll', 'llama-common.dll', 'ggml*.dll', 'libomp.dll', 'LICENSE*')
    foreach ($pattern in $patterns) {
        Get-ChildItem -Path $bundle -Filter $pattern -File | Copy-Item -Destination $output.FullName -Force
    }
    if ($Backend -ne 'cpu' -and -not (Test-Path (Join-Path $output.FullName "ggml-$Backend.dll"))) {
        throw "The $Backend backend DLL is missing from the runtime."
    }
    if ($Backend -eq 'cuda') {
        $cuda = Join-Path $temp 'cuda'
        Expand-VerifiedArchive 'cudart-llama-bin-win-cuda-12.4-x64.zip' `
            '8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e32ae1d6' $cuda
        foreach ($dll in @('cudart64_12.dll', 'cublas64_12.dll', 'cublasLt64_12.dll')) {
            $file = Get-ChildItem $cuda -Recurse -File -Filter $dll | Select-Object -First 1
            if (-not $file) { throw "Missing CUDA runtime DLL: $dll" }
            Copy-Item -LiteralPath $file.FullName -Destination $output.FullName -Force
        }
        @(
            'This package includes CUDA 12.4 runtime libraries from NVIDIA Corporation.'
            'NVIDIA CUDA Toolkit license and third-party notices:'
            'https://docs.nvidia.com/cuda/archive/12.4.1/eula/index.html'
        ) | Set-Content -Path (Join-Path $output.FullName 'LICENSE-NVIDIA-CUDA.txt') -Encoding utf8
    }
    $license = Join-Path $output.FullName 'LICENSE-llama.cpp'
    Invoke-WebRequest -UseBasicParsing -Uri "https://raw.githubusercontent.com/ggml-org/llama.cpp/$version/LICENSE" -OutFile $license
    $launcher = Join-Path $output.FullName 'llama-completion.exe'
    & $launcher --version
    if ($LASTEXITCODE -ne 0) { throw "llama.cpp failed to start (exit $LASTEXITCODE)." }
} finally {
    Remove-Item -LiteralPath $temp -Recurse -Force
}
