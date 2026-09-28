@echo off
rem Crew launcher for Windows
set "DIR=%~dp0"
set "PYTHONPATH=%DIR%;%PYTHONPATH%"
rem "exit /b" with no number keeps Crew's own exit code (%errorlevel% would be read before Crew runs).
where py >nul 2>nul && (py -3 -m crewlib %* & exit /b)
python -m crewlib %*
