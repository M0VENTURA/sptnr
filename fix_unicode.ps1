# Fix broken Unicode characters in popularity.py
$filepath = "c:\Script\Github\sptnr\popularity.py"

# Read with UTF-8 encoding
$content = Get-Content -Path $filepath -Raw -Encoding UTF8

Write-Host "Original file size: $($content.Length) chars" -ForegroundColor Cyan

# Fix broken Unicode characters
$content = $content -replace 'âœ"', '✓'
$content = $content -replace 'â"˜', '✗'
$content = $content -replace 'â±', '⏱'
$content = $content -replace 'âš ', '⚠'
$content = $content -replace 'âœ…', '✅'

Write-Host "Fixed Unicode characters" -ForegroundColor Green

# Write back with UTF-8 encoding
Set-Content -Path $filepath -Value $content -Encoding UTF8 -NoNewline

Write-Host "✓ File updated successfully" -ForegroundColor Green
