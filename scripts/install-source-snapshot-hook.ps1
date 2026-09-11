$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$hookValue = (& git -C $root rev-parse --git-path hooks/post-commit).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Not a Git working repository.' }
if ([IO.Path]::IsPathRooted($hookValue)) { $hook = [IO.Path]::GetFullPath($hookValue) }
else { $hook = [IO.Path]::GetFullPath((Join-Path $root $hookValue)) }
if (-not $hook.StartsWith($root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'A shared external hooks directory is configured; it was not changed.'
}
if (Test-Path -LiteralPath $hook) {
    $existing = [IO.File]::ReadAllText($hook)
    if ($existing.Contains('# kaic-source-snapshot')) { Write-Output 'Source snapshot hook already installed.'; exit 0 }
    throw 'An existing post-commit hook was preserved; integration is required.'
}
$content = @'
#!/bin/sh
# kaic-source-snapshot
root="$(git rev-parse --show-toplevel)" || exit 0
if test -f "$root/.omo/source-snapshot-config.json"; then
  powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$root/scripts/update-source-snapshot.ps1" || printf '%s\n' 'Source snapshot update blocked; the local commit is preserved.'
fi
exit 0
'@
[IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($hook)) | Out-Null
[IO.File]::WriteAllText($hook, $content.Replace("`r`n", "`n") + "`n", [Text.UTF8Encoding]::new($false))
Write-Output 'Source snapshot post-commit hook installed.'
