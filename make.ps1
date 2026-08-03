<#
.SYNOPSIS
    Windows entrypoint for the Odoo Atlas developer targets.

.DESCRIPTION
    GNU make is not installed on Windows by default, and requiring it would put a
    package manager between a reviewer and a running system. This script mirrors
    the Makefile target-for-target so both platforms share one vocabulary.

    Every target delegates to Docker; no local Python interpreter is needed.

.EXAMPLE
    .\make.ps1 init
    .\make.ps1 up
    .\make.ps1 check
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string] $Target = 'help'
)

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

$Compose = @('docker', 'compose')
# --no-deps keeps the tools container from dragging PostgreSQL up with it.
$Tools = $Compose + @('--profile', 'tools', 'run', '--rm', '--no-deps', 'atlas-tools')

function Invoke-Checked {
    param([string[]] $Command)
    Write-Host "> $($Command -join ' ')" -ForegroundColor DarkGray
    & $Command[0] @($Command[1..($Command.Length - 1)])
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE : $($Command -join ' ')"
    }
}

function Get-EnvValue {
    param([string] $Name, [string] $Default)
    if (Test-Path '.env') {
        $line = Select-String -Path '.env' -Pattern "^\s*$Name\s*=\s*(.+)$" | Select-Object -First 1
        if ($line) { return $line.Matches[0].Groups[1].Value.Trim() }
    }
    return $Default
}

function Show-Help {
    Write-Host 'Odoo Atlas - available targets:' -ForegroundColor White
    @(
        @('init',        'Create .env from the template if it does not exist'),
        @('up',          'Build and start the full stack in the background'),
        @('down',        'Stop the stack, keeping all data'),
        @('restart',     'Restart every service'),
        @('ps',          'Show service status and health'),
        @('logs',        'Follow logs from every service'),
        @('build',       'Rebuild images without starting anything'),
        @('rebuild',     'Rebuild images from scratch, ignoring the layer cache'),
        @('shell-atlas', 'Open a shell in the Atlas engine container'),
        @('shell-odoo',  'Open a shell in the Odoo container'),
        @('psql-atlas',  'Open psql against the Atlas (pgvector) database'),
        @('psql-odoo',   'Open psql against the Odoo database'),
        @('lint',        'Run ruff (lint + format check)'),
        @('format',      'Apply ruff formatting and safe autofixes'),
        @('type',        'Run mypy in strict mode'),
        @('imports',     'Verify the architectural layering contracts'),
        @('test',        'Run the offline test suite (unit + contract)'),
        @('test-integration', 'Run integration tests against the running PostgreSQL'),
        @('test-all',    'Run every test tier with the coverage gate'),
        @('test-odoo',   'Install the addon on a throwaway database and run its tests'),
        @('eval',        'Score retrieval against the golden set and gate on it'),
        @('bench',       'Sweep HNSW parameters and report recall and latency'),
        @('audit',       'Check dependencies for vulnerabilities and licence conflicts'),
        @('migrate',     'Apply Alembic migrations to the Atlas database'),
        @('check',       'Run everything CI runs'),
        @('clean',       'Stop the stack and DELETE all data')
    ) | ForEach-Object {
        Write-Host ('  {0,-14}' -f $_[0]) -ForegroundColor Cyan -NoNewline
        Write-Host $_[1]
    }
}

switch ($Target) {
    'help' { Show-Help }

    'init' {
        if (Test-Path '.env') {
            Write-Host '.env already exists - leaving it alone' -ForegroundColor Yellow
        }
        else {
            Copy-Item '.env.example' '.env'
            Write-Host 'created .env from .env.example' -ForegroundColor Green
        }
    }

    'up' {
        Invoke-Checked ($Compose + @('up', '--build', '--detach'))
        $odooPort = Get-EnvValue 'ODOO_PORT' '8069'
        $apiPort = Get-EnvValue 'ATLAS_API_PORT' '8000'
        Write-Host ''
        Write-Host "  Odoo        http://localhost:$odooPort   (admin / admin)" -ForegroundColor Green
        Write-Host "  Atlas API   http://127.0.0.1:$apiPort/docs" -ForegroundColor Green
        Write-Host ''
        Write-Host '  First boot initialises the Odoo database and can take a few minutes.'
        Write-Host '  Follow it with: .\make.ps1 logs'
    }

    'down'    { Invoke-Checked ($Compose + @('down')) }
    'restart' { Invoke-Checked ($Compose + @('restart')) }
    'ps'      { Invoke-Checked ($Compose + @('ps')) }
    'logs'    { Invoke-Checked ($Compose + @('logs', '--follow', '--tail=100')) }
    'build'   { Invoke-Checked ($Compose + @('build')) }
    'rebuild' { Invoke-Checked ($Compose + @('build', '--no-cache')) }

    'shell-atlas' { Invoke-Checked ($Compose + @('exec', 'atlas-api', '/bin/bash')) }
    'shell-odoo'  { Invoke-Checked ($Compose + @('exec', 'odoo', '/bin/bash')) }

    'psql-atlas' {
        $user = Get-EnvValue 'POSTGRES_USER' 'odoo'
        $db = Get-EnvValue 'ATLAS_DB_NAME' 'atlas'
        Invoke-Checked ($Compose + @('exec', 'postgres', 'psql', '-U', $user, '-d', $db))
    }
    'psql-odoo' {
        $user = Get-EnvValue 'POSTGRES_USER' 'odoo'
        $db = Get-EnvValue 'ODOO_DB_NAME' 'odoo'
        Invoke-Checked ($Compose + @('exec', 'postgres', 'psql', '-U', $user, '-d', $db))
    }

    'lint' {
        Invoke-Checked ($Tools + @('ruff', 'check', '.'))
        Invoke-Checked ($Tools + @('ruff', 'format', '--check', '.'))
    }
    'format' {
        Invoke-Checked ($Tools + @('ruff', 'check', '--fix', '.'))
        Invoke-Checked ($Tools + @('ruff', 'format', '.'))
    }
    'type'    { Invoke-Checked ($Tools + @('mypy')) }
    'imports' { Invoke-Checked ($Tools + @('lint-imports')) }
    'test'    { Invoke-Checked ($Tools + @('pytest', '-m', 'unit or contract', '--cov', '--cov-report=term-missing', '--cov-fail-under=0')) }

    'test-integration' {
        $dsn = "postgresql://$(Get-EnvValue 'POSTGRES_USER' 'odoo'):$(Get-EnvValue 'POSTGRES_PASSWORD' 'odoo_dev_password')@postgres:5432/postgres"
        Invoke-Checked ($Compose + @('--profile', 'tools', 'run', '--rm', '-e', "ATLAS_TEST_DATABASE_URL=$dsn", 'atlas-tools', 'pytest', '-m', 'integration', '--no-cov'))
    }

    'test-all' {
        $dsn = "postgresql://$(Get-EnvValue 'POSTGRES_USER' 'odoo'):$(Get-EnvValue 'POSTGRES_PASSWORD' 'odoo_dev_password')@postgres:5432/postgres"
        Invoke-Checked ($Compose + @('--profile', 'tools', 'run', '--rm', '-e', "ATLAS_TEST_DATABASE_URL=$dsn", 'atlas-tools', 'pytest', '-m', 'unit or contract or integration', '--cov', '--cov-report=term-missing'))
    }

    # The addon runs under Odoo's own unittest runner, not pytest, and needs a
    # database with the module installed. Each run starts from a dropped database
    # so a passing result means "installs cleanly and passes", never "still passes
    # on a database someone has been poking at".
    'test-odoo' {
        $user = Get-EnvValue 'POSTGRES_USER' 'odoo'
        $password = Get-EnvValue 'POSTGRES_PASSWORD' 'odoo_dev_password'
        $database = 'odoo_atlas_test'
        Invoke-Checked ($Compose + @('up', '--detach', 'postgres'))
        Invoke-Checked ($Compose + @('exec', '-T', 'postgres', 'psql', '-U', $user, '-d', 'postgres',
            '-c', "DROP DATABASE IF EXISTS $database WITH (FORCE)"))
        Invoke-Checked ($Compose + @('run', '--rm', '--entrypoint', 'odoo',
            '-e', "PGPASSWORD=$password", 'odoo',
            '--db_host', 'postgres', '--db_port', '5432', '--db_user', $user,
            '--database', $database, '--init', 'odoo_atlas', '--without-demo',
            '--test-enable', '--test-tags', '/odoo_atlas',
            '--stop-after-init', '--max-cron-threads=0', '--log-level=test'))
    }

    # Offline: a file corpus, a deterministic embedder, an in-memory store. No
    # services and no API key, which is the only reason CI can gate on it.
    'eval' {
        Invoke-Checked ($Tools + @('python', '-m', 'atlas.interfaces.evaluate'))
    }

    'eval-live' {
        Invoke-Checked ($Compose + @('exec', 'atlas-api', 'python', '-m', 'atlas.interfaces.evaluate', '--live'))
    }

    # Needs a database it may create and drop tables in — never the corpus.
    'bench' {
        $user = Get-EnvValue 'POSTGRES_USER' 'odoo'
        $password = Get-EnvValue 'POSTGRES_PASSWORD' 'odoo_dev_password'
        $database = 'atlas_bench'
        Invoke-Checked ($Compose + @('up', '--detach', 'postgres'))
        & $Compose[0] @($Compose[1..($Compose.Length - 1)] + @('exec', '-T', 'postgres',
            'createdb', '-U', $user, $database)) 2>$null
        Invoke-Checked ($Compose + @('exec', '-T', 'postgres', 'psql', '-U', $user,
            '-d', $database, '-c', 'CREATE EXTENSION IF NOT EXISTS vector'))
        $dsn = "postgresql://${user}:${password}@postgres:5432/$database"
        $commit = (& git rev-parse HEAD)
        $branch = (& git rev-parse --abbrev-ref HEAD)
        $dirty = if ((& git status --porcelain)) { '1' } else { '' }
        Invoke-Checked ($Compose + @('--profile', 'tools', 'run', '--rm',
            '-e', "ATLAS_BENCH_DATABASE_URL=$dsn",
            '-e', "ATLAS_BENCH_COMMIT=$commit",
            '-e', "ATLAS_BENCH_BRANCH=$branch",
            '-e', "ATLAS_BENCH_DIRTY=$dirty",
            'atlas-tools', 'python', '-m', 'benchmarks.run_all'))
    }

    'audit' {
        Invoke-Checked ($Tools + @('python', 'scripts/audit_dependencies.py'))
        Invoke-Checked ($Tools + @('python', 'scripts/check_licences.py'))
        Invoke-Checked ($Tools + @('python', 'scripts/check_versions.py'))
    }

    'check' {
        & $PSCommandPath 'lint'
        & $PSCommandPath 'type'
        & $PSCommandPath 'imports'
        & $PSCommandPath 'test'
        & $PSCommandPath 'eval'
        & $PSCommandPath 'test-odoo'
    }

    'clean' {
        Write-Host 'This destroys the Odoo and Atlas databases and the Odoo filestore.' -ForegroundColor Red
        $answer = Read-Host "Type 'yes' to continue"
        if ($answer -ne 'yes') {
            Write-Host 'Aborted.' -ForegroundColor Yellow
            return
        }
        Invoke-Checked ($Compose + @('down', '--volumes', '--remove-orphans'))
    }

    default {
        Write-Host "Unknown target '$Target'." -ForegroundColor Red
        Show-Help
        exit 1
    }
}
