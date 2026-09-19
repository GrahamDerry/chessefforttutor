@echo off
REM Resume the M2 pipeline chain. Run detached so it survives the Claude session.
cd /d "%~dp0.."
set TUTOR_DB=data\tutor.db
set STOCKFISH_PATH=C:/Users/hylke/engines/sf19/stockfish/stockfish-windows-x86-64-universal.exe
set STOCKFISH_HASH_MB=64
set PYTHONUNBUFFERED=1
if "%WORKERS%"=="" set WORKERS=1
echo === m2 chain start %DATE% %TIME% workers=%WORKERS% >> data\m2_run.log
.venv\Scripts\python -m pipeline analyze --workers %WORKERS% >> data\m2_run.log 2>&1
if errorlevel 1 goto done
.venv\Scripts\python -m pipeline forks >> data\m2_run.log 2>&1
if errorlevel 1 goto done
.venv\Scripts\python -m pipeline scenarios >> data\m2_run.log 2>&1
if errorlevel 1 goto done
.venv\Scripts\python -m pipeline report >> data\m2_run.log 2>&1
:done
echo === m2 chain exit=%ERRORLEVEL% %DATE% %TIME% >> data\m2_run.log
