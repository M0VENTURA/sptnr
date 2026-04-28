# PowerShell script to apply single detection optimizations - FIXED
$filepath = "c:\Script\Github\sptnr\popularity.py"

# Read the file with UTF-8 encoding
$content = Get-Content -Path $filepath -Raw -Encoding UTF8

Write-Host "✓ Read popularity.py" -ForegroundColor Green

# Change 1: Track MusicBrainz as medium confidence
$oldMB = '                if result:$([Environment]::NewLine)                    single_sources.append("musicbrainz")$([Environment]::NewLine)                    medium_confidence_sources.append("musicbrainz")$([Environment]::NewLine)                    log_info('
$newMB = @'
                if result:
                    single_sources.append("musicbrainz")
                    medium_confidence_sources.append("musicbrainz")
                    log_info(
'@
$content = $content -replace [regex]::Escape($oldMB), $newMB

Write-Host "✓ Fixed MusicBrainz tracking" -ForegroundColor Green

# Change 2: Track MusicBrainz video as medium confidence
$content = $content -replace `
    '\$\(\[Environment\]::NewLine\)', "`n"

Write-Host "✓ Fixed all newline placeholders" -ForegroundColor Green

# Write the modified content back
Set-Content -Path $filepath -Value $content -Encoding UTF8 -NoNewline

Write-Host "`n✅ Fixed newline issues!" -ForegroundColor Green
