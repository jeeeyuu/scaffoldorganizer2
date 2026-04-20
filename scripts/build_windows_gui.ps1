$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

# Build the single-file GUI launcher. `--add-data` folds the HTML/CSS/JS
# frontend into the bundle so index.html resolves from _MEIPASS at runtime.
# The backend prompt .md files are bundled too — they're the offline fallback
# for any role whose prompt_id isn't set on the OpenAI dashboard.
python -m PyInstaller `
    --onefile `
    --windowed `
    --name ScaffoldOrganizer2 `
    --add-data "gui;gui" `
    --add-data "backend/prompts;backend/prompts" `
    scripts/run_gui.py

# Stage a config directory next to the exe. config_example.json is copied
# so the end user can rename to config.json and fill secrets + prompt IDs.
# The real config.json stays out of dist/ (it's gitignored anyway).
New-Item -ItemType Directory -Force -Path "dist/config" | Out-Null
Copy-Item "config/config_example.json" "dist/config/config_example.json" -Force
Copy-Item "config/config.schema.json" "dist/config/config.schema.json" -Force

Write-Host ""
Write-Host "Build complete. Next:"
Write-Host "  1. cd dist"
Write-Host "  2. copy config\config_example.json config\config.json"
Write-Host "  3. Fill openai_api_key + ai_roles.*.prompt_id + wsl_backend_entrypoint"
Write-Host "  4. Run .\ScaffoldOrganizer2.exe"
