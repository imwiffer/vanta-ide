@echo off
rem Vanta IDE - opens the native desktop window (WebView2, no browser chrome).
rem Console stays closed; anything that goes wrong lands in logs\desktop.log.
setlocal
set "HERE=%~dp0"
set "ROOT=%HERE%"
set "PYW=%HERE%.venv\Scripts\pythonw.exe"
if not exist "%PYW%" set "PYW=%HERE%..\airllm\.venv\Scripts\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw"
if "%~1"=="--admin" (
    powershell -NoProfile -Command "Start-Process '%PYW%' -ArgumentList '""%HERE%desktop.py""' -WorkingDirectory '%HERE%' -Verb RunAs"
    exit /b
)
start "Vanta IDE" "%PYW%" "%HERE%desktop.py" %*
