@echo off
REM Windows launcher for the op CLI.
REM Put this directory on your PATH to run `op` from anywhere.
REM
REM Structured with labels rather than parenthesised && blocks on purpose:
REM inside a block, %errorlevel% is expanded when the block is parsed rather
REM than when it runs, so the launcher would report a stale exit code and
REM callers doing `op ... || handle-failure` would never see a failure.

where /q py.exe
if errorlevel 1 goto try_python
py -3 "%~dp0op.py" %*
exit /b %errorlevel%

:try_python
where /q python.exe
if errorlevel 1 goto no_python
python "%~dp0op.py" %*
exit /b %errorlevel%

:no_python
echo op: Python 3.8+ not found on PATH. Install it from https://python.org 1>&2
exit /b 1
