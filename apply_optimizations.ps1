# PowerShell script to apply single detection optimizations
$filepath = "c:\Script\Github\sptnr\popularity.py"

# Read the file with UTF-8 encoding
$content = Get-Content -Path $filepath -Raw -Encoding UTF8

Write-Host "✓ Read popularity.py" -ForegroundColor Green

# Change 1: Track MusicBrainz as medium confidence
$content = $content -replace `
    '(?s)(if result:\s+single_sources\.append\("musicbrainz"\)\s+log_info)', `
    'if result:$([Environment]::NewLine)                    single_sources.append("musicbrainz")$([Environment]::NewLine)                    medium_confidence_sources.append("musicbrainz")$([Environment]::NewLine)                    log_info'

Write-Host "✓ Change 1: Added MusicBrainz to medium confidence tracking" -ForegroundColor Green

# Change 2: Track MusicBrainz video as medium confidence  
$content = $content -replace `
    '(?s)(if has_video:\s+single_sources\.append\("musicbrainz_video"\))', `
    'if has_video:$([Environment]::NewLine)                        single_sources.append("musicbrainz_video")$([Environment]::NewLine)                        medium_confidence_sources.append("musicbrainz_video")'

Write-Host "✓ Change 2: Added MusicBrainz video to medium confidence tracking" -ForegroundColor Green

# Change 3: Track MusicBrainz compilation as medium confidence
$content = $content -replace `
    '(?s)(if on_compilations:\s+single_sources\.append\("musicbrainz_compilation"\))', `
    'if on_compilations:$([Environment]::NewLine)                        single_sources.append("musicbrainz_compilation")$([Environment]::NewLine)                        medium_confidence_sources.append("musicbrainz_compilation")'

Write-Host "✓ Change 3: Added MusicBrainz compilation to medium confidence tracking" -ForegroundColor Green

# Change 4: Add early exit after MusicBrainz
$earlyExitMB = @'
                except Exception as e:
                    log_debug(f"   MusicBrainz compilation check error for {title}: {e}")
                    
                # Check if 2 medium sources = high confidence (early exit)
                if len(medium_confidence_sources) >= 2:
                    log_info(f"   🎯 EARLY EXIT: 2 medium sources detected ({medium_confidence_sources}), promoting to HIGH")
                    return {
                        "sources": list(dict.fromkeys(single_sources)),
                        "confidence": "high",
                        "is_single": True
                    }
        except TimeoutError as e:
'@

$content = $content -replace `
    '(?s)(except Exception as e:\s+log_debug\(f"   MusicBrainz compilation check error[^\n]+\)\s+)(except TimeoutError as e:)', `
    ($earlyExitMB -replace '\r\n', [Environment]::NewLine)

Write-Host "✓ Change 4: Added early exit after MusicBrainz" -ForegroundColor Green

# Change 5: Track Spotify as medium confidence
$content = $content -replace `
    '(?s)(if matched_release:\s+single_sources\.append\("spotify"\)\s+album_info)', `
    'if matched_release:$([Environment]::NewLine)                single_sources.append("spotify")$([Environment]::NewLine)                medium_confidence_sources.append("spotify")$([Environment]::NewLine)                album_info'

Write-Host "✓ Change 5: Added Spotify to medium confidence tracking" -ForegroundColor Green

# Change 6: Track Discogs video as medium confidence
$content = $content -replace `
    '(?s)(if result:\s+single_sources\.append\("discogs_video"\)\s+log_info\(f"   [^\n]+Discogs confirms music video)', `
    'if result:$([Environment]::NewLine)                    single_sources.append("discogs_video")$([Environment]::NewLine)                    medium_confidence_sources.append("discogs_video")$([Environment]::NewLine)                    log_info(f"   âœ" Discogs confirms music video'

Write-Host "✓ Change 6: Added Discogs video to medium confidence tracking" -ForegroundColor Green

# Change 7: Track iterative z-score as medium confidence
$content = $content -replace `
    '(?s)(if iterative_zscore_passed:\s+single_sources\.append\("iterative_zscore"\)\s+log_info)', `
    'if iterative_zscore_passed:$([Environment]::NewLine)                single_sources.append("iterative_zscore")$([Environment]::NewLine)                medium_confidence_sources.append("iterative_zscore")$([Environment]::NewLine)                log_info'

Write-Host "✓ Change 7: Added iterative z-score to medium confidence tracking" -ForegroundColor Green

# Change 8: Update final confidence logic
$oldConfidence = @'
    if has_discogs_single:
        single_confidence = "high"
    elif has_iterative_zscore or has_other_sources or has_discogs_video:
        single_confidence = "medium"
    else:
        single_confidence = "low"
'@

$newConfidence = @'
    # NEW RULE: 2 medium sources = high confidence
    if has_discogs_single or len(medium_confidence_sources) >= 2:
        single_confidence = "high"
    elif has_iterative_zscore or has_other_sources or has_discogs_video:
        single_confidence = "medium"
    else:
        single_confidence = "low"
'@

$content = $content -replace [regex]::Escape($oldConfidence), ($newConfidence -replace '\r\n', [Environment]::NewLine)

Write-Host "✓ Change 8: Updated final confidence logic (2 medium = high)" -ForegroundColor Green

# Write the modified content back
Set-Content -Path $filepath -Value $content -Encoding UTF8 -NoNewline

Write-Host "`n✅ All optimizations applied successfully!" -ForegroundColor Green
Write-Host "`nChanges made:" -ForegroundColor Cyan
Write-Host "  - Added medium_confidence_sources tracking to all medium confidence sources"
Write-Host "  - Added early exit after MusicBrainz (if 2 medium sources)"
Write-Host "  - Updated final confidence: 2 medium = high"
