# Optional: build a Windows executable folder with PyInstaller.
#
#   powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
#
# Result: dist\ascii-music-player\ascii-music-player.exe  (run it from PowerShell / Windows Terminal)
#
# The executable bundles Python, OpenCV, NumPy, Rich, httpx and yt-dlp.
# It does NOT bundle mpv or FFmpeg: they must still be installed on the target machine
# (see README: winget install shinchiro.mpv). Run `ascii-music-player.exe --doctor` to check.
#
# A one-folder build (--onedir) is used on purpose: it starts faster and is more reliable than
# --onefile, which unpacks itself to a temp folder on every launch.

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

python -m pip install -e ".[package]"
python -m PyInstaller --noconfirm --clean --onedir `
    --name ascii-music-player `
    --collect-all yt_dlp `
    --collect-submodules cv2 `
    run.py

Write-Host ""
Write-Host "Built: dist\ascii-music-player\ascii-music-player.exe"
Write-Host "Smoke test:"
& ".\dist\ascii-music-player\ascii-music-player.exe" --version
& ".\dist\ascii-music-player\ascii-music-player.exe" --doctor
