#!/usr/bin/env pwsh
# Portable PostgreSQL setup for local development.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$zip = Join-Path $root "postgresql-15.19-1-windows-x64-binaries.zip"
$pgsql = Join-Path $root "pgsql"
$pgdata = Join-Path $root "pgdata"
$env:PGDATA = $pgdata

if (-not (Test-Path $zip)) {
    throw "Archive not found: $zip"
}

if (-not (Test-Path $pgsql)) {
    Write-Host "Extracting PostgreSQL..."
    Expand-Archive -Path $zip -DestinationPath $root -Force
}

$initdb = Join-Path $pgsql "bin\initdb.exe"
$pg_ctl = Join-Path $pgsql "bin\pg_ctl.exe"
$psql = Join-Path $pgsql "bin\psql.exe"
$createdb = Join-Path $pgsql "bin\createdb.exe"

if (-not (Test-Path $pgdata)) {
    Write-Host "Initializing database cluster..."
    & $initdb -U postgres -A trust -E UTF8 -D $pgdata
    if ($LASTEXITCODE -ne 0) { throw "initdb failed" }
}

Write-Host "Starting PostgreSQL..."
& $pg_ctl -D $pgdata -l (Join-Path $pgdata "log") start -w
if ($LASTEXITCODE -ne 0) { throw "pg_ctl start failed" }

Write-Host "Creating user and database..."
& $psql -U postgres -h localhost -p 5432 -c "DO \$\$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='hq_user') THEN CREATE USER hq_user WITH PASSWORD 'change_me' SUPERUSER; END IF; END \$\$;" postgres
& $psql -U postgres -h localhost -p 5432 -c "ALTER USER hq_user WITH PASSWORD 'change_me';" postgres
# createdb returns non-zero if DB already exists, swallow it
& $createdb -U postgres -h localhost -p 5432 -O hq_user school_hq_db 2>$null

Write-Host "PostgreSQL is running on localhost:5432"
Write-Host "  User: hq_user"
Write-Host "  Password: change_me"
Write-Host "  Database: school_hq_db"

