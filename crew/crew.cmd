@echo off
rem Crew launcher for Windows
set "DIR=%~dp0"
set "PYTHONPATH=%DIR%;%PYTHONPATH%"
rem "exit /b" with no number keeps Crew's own exit code (%errorlevel% would be read before Crew runs).
rem -X utf8: what Crew prints (a tick, any language) also goes to a file or a pipe on a Windows set to an ANSI code page.
where py >nul 2>nul && (py -3 -X utf8 -m crewlib %* & exit /b)
python -X utf8 -m crewlib %*
