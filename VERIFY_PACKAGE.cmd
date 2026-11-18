@echo off
setlocal
set "ROOT=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$root=$env:ROOT; $sum=Join-Path $root '00_READ_ME_FIRST\SHA256SUMS'; if(!(Test-Path -LiteralPath $sum)){Write-Output 'BLOCKED: SHA256SUMS missing'; exit 2}; $bad=0; foreach($line in Get-Content -LiteralPath $sum){ if([string]::IsNullOrWhiteSpace($line)){continue}; $parts=$line -split '  ',2; if($parts.Count -ne 2){Write-Output ('INVALID: '+$line); $bad=1; continue}; $file=Join-Path $root $parts[1]; if(!(Test-Path -LiteralPath $file)){Write-Output ('MISSING: '+$parts[1]); $bad=1; continue}; $actual=(Get-FileHash -Algorithm SHA256 -LiteralPath $file).Hash.ToLower(); if($actual -ne $parts[0].ToLower()){Write-Output ('MISMATCH: '+$parts[1]); $bad=1} }; if($bad){exit 2}; Write-Output 'PASS: package SHA256 verified'"
endlocal & exit /b %ERRORLEVEL%
