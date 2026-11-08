#!/usr/bin/env pwsh
$root = Split-Path -Parent $PSScriptRoot
. (Join-Path $root ".venv\Scripts\Activate.ps1")
cd $root
python -m app.main
