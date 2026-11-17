@echo off
rem Vanta Console - chat with Vanta from your terminal (same engines as the
rem chat dock). Attaches to a running Vanta server or starts one.
setlocal
set "HERE=%~dp0"
set "PY=%HERE%.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%HERE%..\airllm\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%HERE%console.py" %*
