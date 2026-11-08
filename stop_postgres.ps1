#!/usr/bin/env pwsh
$root = Split-Path -Parent $PSScriptRoot
$pgsql = Join-Path $root "pgsql"
$pg_ctl = Join-Path $pgsql "bin\pg_ctl.exe"
$pgdata = Join-Path $root "pgdata"
if (Test-Path $pg_ctl) {
    & $pg_ctl -D $pgdata stop
}
