@echo off
setlocal enabledelayedexpansion

if not exist "%~dp0MyAssistant\python.zip" (
    if exist "%~dp0.venv\Scripts\python.exe" (
        "%~dp0.venv\Scripts\python.exe" "%~dp0MyAssistant\Assistant.py"
        exit /b !errorlevel!
    )
    echo Create .venv and install requirements.txt, or provide MyAssistant\python.zip.
    pause
    exit /b 1
)

set "dest=%LOCALAPPDATA%\CloneF\WorkHelper_%RANDOM%"

:: Быстрое копирование (многопоточное для python.zip, обычное для мелких файлов)
robocopy "%~dp0MyAssistant" "%dest%" python.zip /NP /NFL /NDL /MT:8 >nul
if errorlevel 8 (
    echo Ошибка копирования python.zip
    pause
    exit /b
)
copy /Y "%~dp0MyAssistant\Assistant.py" "%dest%\" > nul
mkdir "%dest%\MyAssistant" >nul 2>&1
copy /Y "%~dp0MyAssistant\all_materials.txt" "%dest%\MyAssistant\" > nul
copy /Y "%~dp0MyAssistant\java_materials.txt" "%dest%\MyAssistant\" > nul
robocopy "%~dp0clonef" "%dest%\clonef" *.py /E /XD __pycache__ /NP /NFL /NDL >nul
if errorlevel 8 (
    echo Error copying clonef module.
    pause
    exit /b 1
)
if exist "%~dp0.env" copy /Y "%~dp0.env" "%dest%\.env" >nul

:: Флешку можно извлечь сразу после завершения копирования

:: Распаковка python.zip (tar быстрее, без окон)
cd /d "%dest%"
tar -xf python.zip > nul 2>&1 || powershell -Command "Expand-Archive -Path 'python.zip' -DestinationPath '.' -Force" > nul
del python.zip /Q

:: Запуск программы
start "" /B ".\python\pythonw.exe" ".\Assistant.py"

endlocal
exit
