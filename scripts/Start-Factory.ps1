$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Push-Location (Join-Path $root 'src/Factory.Web')
try {
    npm.cmd ci --no-fund --no-audit
    if ($LASTEXITCODE) { throw 'Dashboard dependency installation failed.' }
    npm.cmd run build
    if ($LASTEXITCODE) { throw 'Dashboard build failed.' }
} finally { Pop-Location }
Push-Location (Join-Path $root 'src/Factory.Api')
try {
    dotnet restore --locked-mode
    if ($LASTEXITCODE) { throw 'Controller restore failed.' }
    dotnet run --no-restore
    if ($LASTEXITCODE) { throw 'Controller exited with an error.' }
} finally { Pop-Location }
