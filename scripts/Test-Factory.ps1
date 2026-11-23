param([switch]$Browser, [switch]$Dispatch, [switch]$ProjectCi)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Push-Location (Join-Path $root 'src/Factory.Api')
try {
    dotnet restore --locked-mode
    if ($LASTEXITCODE) { throw 'Controller restore failed.' }
    dotnet build --no-restore
    if ($LASTEXITCODE) { throw 'Controller build failed.' }
} finally { Pop-Location }
$contractProject = Join-Path $root 'tests/Factory.ContractTests'
if ($PSVersionTable.PSVersion -ge [version]'7.4') {
    $samples = Join-Path ([IO.Path]::GetTempPath()) ('factory-contract-' + [guid]::NewGuid().ToString('N'))
    $schema = Join-Path $root 'contracts/controlled-v1.schema.json'
    $names = @('assignment','worker-result','receipt','project-policy','phase-context','gate-evaluation','blueprint-context')
    try {
        dotnet run --project $contractProject -- $samples
        if ($LASTEXITCODE) { throw 'Controlled worker contract checks failed.' }
        foreach ($name in $names) {
            $sample = Join-Path $samples "$name.json"
            if (-not (Test-Json -LiteralPath $sample -SchemaFile $schema)) { throw "Schema rejected $name." }
        }
        $bad = Get-Content -Raw (Join-Path $samples 'project-policy.json') | ConvertFrom-Json
        $bad.maxRepairs = -1
        if (Test-Json -Json ($bad | ConvertTo-Json) -SchemaFile $schema -ErrorAction SilentlyContinue) {
            throw 'Schema accepted a negative repair budget.'
        }
        Write-Output 'Passed: seven serialized contract examples and rejection of invalid policy'
    } finally {
        foreach ($name in $names) {
            $file = Join-Path $samples "$name.json"
            if (Test-Path -LiteralPath $file) { Remove-Item -LiteralPath $file }
        }
        if (Test-Path -LiteralPath $samples) { [IO.Directory]::Delete($samples) }
    }
} else {
    dotnet run --project $contractProject
    if ($LASTEXITCODE) { throw 'Controlled worker contract checks failed.' }
}
$previous = $env:FACTORY_BROWSER_TEST
try {
    if ($Browser) {
        Push-Location (Join-Path $root 'src/Factory.Web')
        try {
            npm.cmd ci --no-fund --no-audit
            if ($LASTEXITCODE) { throw 'Dashboard dependency installation failed.' }
            npm.cmd run check
            if ($LASTEXITCODE) { throw 'Dashboard type check failed.' }
            npm.cmd run build
            if ($LASTEXITCODE) { throw 'Dashboard build failed.' }
            node node_modules/playwright-core/cli.js install chromium --only-shell
            if ($LASTEXITCODE) { throw 'Browser installation failed.' }
        } finally { Pop-Location }
        $env:FACTORY_BROWSER_TEST = '1'
    }
    python (Join-Path $root 'tests/test_slice.py')
    if ($LASTEXITCODE) { throw 'Qualification tests failed.' }
    python (Join-Path $root 'tests/test_pilot.py')
    if ($LASTEXITCODE) { throw 'Pilot evaluation qualification failed.' }
    python (Join-Path $root 'tests/test_slack.py')
    if ($LASTEXITCODE) { throw 'Slack fixture qualification failed.' }
    python (Join-Path $root 'tests/test_restore.py')
    if ($LASTEXITCODE) { throw 'SQLite restore qualification failed.' }
    python (Join-Path $root 'tests/test_funding.py')
    if ($LASTEXITCODE) { throw 'Funding qualification failed.' }
    python (Join-Path $root 'tests/test_github.py')
    if ($LASTEXITCODE) { throw 'GitHub fixture qualification failed.' }
    if ($ProjectCi -or $Dispatch) {
        python (Join-Path $root 'tests/test_codex_workspace.py')
        if ($LASTEXITCODE) { throw 'Scoped repository tool qualification failed.' }
        python (Join-Path $root 'tests/test_environment_leases.py')
        if ($LASTEXITCODE) { throw 'Workspace lease qualification failed.' }
        dotnet build (Join-Path $root 'tests/Factory.SelfDevelopmentFixture') -p:RestoreLockedMode=true
        if ($LASTEXITCODE) { throw 'Self-development fixture build failed.' }
        python (Join-Path $root 'tests/qualify_self_development.py')
        if ($LASTEXITCODE) { throw 'Factory-source release qualification failed.' }
        python (Join-Path $root 'tests/test_project_ci.py')
        if ($LASTEXITCODE) { throw 'Source-bound project CI qualification failed.' }
    }
} finally { $env:FACTORY_BROWSER_TEST = $previous }

if ($Dispatch) {
    Push-Location (Join-Path $root 'src/Factory.Dispatch')
    try {
        npm.cmd ci --ignore-scripts --no-fund --no-audit
        if ($LASTEXITCODE) { throw 'Dispatcher dependency installation failed.' }
        npm.cmd run check
        if ($LASTEXITCODE) { throw 'Dispatcher type check failed.' }
    } finally { Pop-Location }
    python (Join-Path $root 'tests/qualify_dispatch.py')
    if ($LASTEXITCODE) { throw 'Durable dispatch qualification failed.' }
    python (Join-Path $root 'tests/qualify_factory_scheduler.py')
    if ($LASTEXITCODE) { throw 'Normal factory-source dispatch qualification failed.' }
    python (Join-Path $root 'tests/qualify_restore.py')
    if ($LASTEXITCODE) { throw 'PostgreSQL restore qualification failed.' }
}
