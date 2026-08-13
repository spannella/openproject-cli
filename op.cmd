@echo off
REM Windows launcher for the op CLI.
REM Put this directory on your PATH to run `op` from anywhere.
where /q py.exe && (py -3 "%~dp0op.py" %* & exit /b %errorlevel%)
where /q python.exe && (python "%~dp0op.py" %* & exit /b %errorlevel%)
echo op: Python 3.8+ not found on PATH. Install it from https://python.org 1>&2
exit /b 1
