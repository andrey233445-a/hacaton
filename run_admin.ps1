#!/usr/bin/env pwsh
$root = Split-Path -Parent $PSScriptRoot
. (Join-Path $root ".venv\Scripts\Activate.ps1")
cd $root
uvicorn app.admin_web.main:app --host 0.0.0.0 --port 8000
