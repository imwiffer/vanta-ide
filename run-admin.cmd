@echo off
rem Vanta IDE - launch with full Administrator privileges.
setlocal
set "HERE=%~dp0"
set "ROOT=%HERE%"
set "PYW=%HERE%.venv\Scripts\pythonw.exe"
if not exist "%PYW%" set "PYW=%HERE%..\airllm\.venv\Scripts\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw"
powershell -NoProfile -Command "Start-Process '%PYW%' -ArgumentList '""%HERE%desktop.py""' -WorkingDirectory '%ROOT%' -Verb RunAs"
