@echo off
set CLOAKBROWSER_AUTO_UPDATE=false
set CLOAKBROWSER_CACHE_DIR=%~dp0.cloakbrowser
set PYTHONPATH=%~dp0src
cd /d %~dp0
echo Starting aistudio-api proxy...
echo Browser will be downloaded automatically on first run.
echo If download fails, please manually download from:
echo   https://github.com/CloakHQ/cloakbrowser/releases
echo and extract to: %CLOAKBROWSER_CACHE_DIR%\
echo.
python -m uvicorn aistudio_api.api.app:app --host 127.0.0.1 --port 8080
pause
