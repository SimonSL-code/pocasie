@echo off
rem Spustenie stranky s pocasim SHMU (Windows). Okno nechajte otvorene, kym stranku pouzivate.
setlocal
cd /d "%~dp0"
title Pocasie SHMU

set "PYEXE="
set "PYARGS="

rem 1) Python z python.org (spustac py)
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul
if not errorlevel 1 (
  set "PYEXE=py"
  set "PYARGS=-3"
  goto run
)

rem 2) python v systemovej ceste (PATH)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul
if not errorlevel 1 (
  set "PYEXE=python"
  goto run
)

rem 3) Miniconda alebo Anaconda
for %%R in ("%USERPROFILE%\MiniConda3" "%USERPROFILE%\anaconda3" "%LOCALAPPDATA%\miniconda3" "%LOCALAPPDATA%\anaconda3" "%ProgramData%\miniconda3" "%ProgramData%\anaconda3") do (
  if exist "%%~R\python.exe" (
    set "PYEXE=%%~R\python.exe"
    set "PATH=%%~R;%%~R\Library\bin;%%~R\Scripts;%PATH%"
    goto run
  )
)

rem 4) Python z python.org bez spustaca
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
  if exist "%%~D\python.exe" set "PYEXE=%%~D\python.exe"
)
if defined PYEXE goto run

echo.
echo Python sa nenasiel.
echo Nainstalujte ho z https://www.python.org/downloads/ (pri instalacii zaskrtnite "Add Python to PATH").
echo.
pause
exit /b 1

:run
echo Spustam stranku s pocasim SHMU ...
echo Python: %PYEXE% %PYARGS%
echo.
"%PYEXE%" %PYARGS% server.py --otvorit %*
echo.
echo Server sa zastavil.
pause
