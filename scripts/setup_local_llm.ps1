# Omni-Organizer v3 - OPTIONAL local LLM setup (Ollama + Vulkan for AMD GPUs without ROCm/CUDA)
# Run manually when you want Tier 2 AI to be fully offline:
#   powershell -ExecutionPolicy Bypass -File scripts\setup_local_llm.ps1 [-Model qwen2.5:3b]
param(
    [string]$Model = "qwen2.5:3b"
)
$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Write-Host "=== 1. Install Ollama (winget, user scope) ===" -ForegroundColor Cyan
if (-not (Get-Command ollama -ErrorAction SilentlyContinue) -and -not (Test-Path "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe")) {
    winget install --id Ollama.Ollama -e --accept-source-agreements --accept-package-agreements
} else {
    Write-Host "Ollama already installed."
}
$Ollama = (Get-Command ollama -ErrorAction SilentlyContinue).Source
if (-not $Ollama) { $Ollama = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" }

Write-Host "=== 2. Enable Vulkan backend and pin discrete GPU (Vulkan device 0) ===" -ForegroundColor Cyan
[Environment]::SetEnvironmentVariable("OLLAMA_VULKAN", "1", "User")
[Environment]::SetEnvironmentVariable("GGML_VK_VISIBLE_DEVICES", "0", "User")
# Keep the model only briefly in VRAM so games are not starved.
[Environment]::SetEnvironmentVariable("OLLAMA_KEEP_ALIVE", "2m", "User")
$env:OLLAMA_VULKAN = "1"; $env:GGML_VK_VISIBLE_DEVICES = "0"; $env:OLLAMA_KEEP_ALIVE = "2m"
Write-Host "Restart the Ollama tray app once so it picks up the new environment variables."

Write-Host "=== 3. Pull model: $Model ===" -ForegroundColor Cyan
& $Ollama pull $Model

Write-Host "=== 4. Smoke test (JSON output) ===" -ForegroundColor Cyan
$body = @{
    model = $Model; stream = $false; format = "json"
    messages = @(@{ role = "user"; content = 'Return {"ok": true} as JSON.' })
} | ConvertTo-Json -Depth 5
try {
    $r = Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/chat" -Method Post -Body $body -ContentType "application/json" -TimeoutSec 120
    Write-Host "Model reply: $($r.message.content)" -ForegroundColor Green
} catch {
    Write-Host "Smoke test failed: $_" -ForegroundColor Red
}
& $Ollama ps
Write-Host "Done. If 'ollama ps' shows 100% CPU, check GPU driver / Vulkan device index." -ForegroundColor Yellow
Write-Host "Then restart the watcher: python src\manage_service.py restart" -ForegroundColor Yellow
