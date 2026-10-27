$ErrorActionPreference = "Stop"

Write-Host "Local Bot Manager - YouTube API key setup" -ForegroundColor Cyan
Write-Host "The key will be stored in your Windows user environment, not in the project."
$secureKey = Read-Host "Paste the new restricted YouTube Data API v3 key" -AsSecureString
$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)

try {
    $plainKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer).Trim()
    if ($plainKey -notmatch '^AIza[0-9A-Za-z_-]{35}$') {
        throw "The value does not look like a valid Google API key. Nothing was saved."
    }

    [Environment]::SetEnvironmentVariable("YOUTUBE_API_KEY", $plainKey, "User")
    Write-Host "YouTube API key saved for this Windows user." -ForegroundColor Green
    Write-Host "Restart the Local Bot Manager backend to load it."
}
finally {
    if ($pointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
    $plainKey = $null
    $secureKey = $null
}
