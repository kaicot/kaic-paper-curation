$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (-not (Test-Path -LiteralPath (Join-Path $root '.git') -PathType Container)) {
    throw 'Run this only in the D-drive working repository, not the source snapshot.'
}
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$record = Get-Content -LiteralPath (Join-Path $root '.omo/runtime/python312-active.json') -Raw | ConvertFrom-Json
$runtime = [IO.Path]::GetFullPath((Join-Path $root $record.runtime_dir))
if (-not $runtime.StartsWith($root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Runtime path escaped the working project.'
}
$pythonExec = Join-Path $runtime 'Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonExec)) { $pythonExec = Join-Path $runtime 'python.exe' }
& $pythonExec -B (Join-Path $root 'pipeline/tools/export_source_snapshot.py') --publish
exit $LASTEXITCODE
