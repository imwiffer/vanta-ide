@echo off
rem Vanta IDE - desktop window with a console attached and WebView2 devtools on.
rem Use this when the frameless/no-console launch misbehaves.
setlocal
set "HERE=%~dp0"
set "PY=%HERE%.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%HERE%..\airllm\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%HERE%desktop.py" --debug %*
