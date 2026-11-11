@echo off
rem Vanta IDE - browser mode (old behaviour). Handy for a second monitor or
rem when WebView2 is misbehaving: .\run-web.cmd
setlocal
set "HERE=%~dp0"
set "PY=%HERE%.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%HERE%..\airllm\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%HERE%server.py" %*
