@echo off
setlocal enabledelayedexpansion

set "dest=%PUBLIC%\WorkHelper_%RANDOM%"

:: Быстрое копирование (многопоточное для python.zip, обычное для мелких файлов)
robocopy "%~dp0MyAssistant" "%dest%" python.zip /NP /NFL /NDL /MT:8 >nul
if errorlevel 8 (
    echo Ошибка копирования python.zip
    pause
    exit /b
)
copy /Y "%~dp0MyAssistant\Assistant.py" "%dest%\" > nul
copy /Y "%~dp0MyAssistant\all_materials.txt" "%dest%\" > nul
copy /Y "%~dp0MyAssistant\java_materials.txt" "%dest%\" > nul

:: Флешку можно извлечь сразу после завершения копирования

:: Распаковка python.zip (tar быстрее, без окон)
cd /d "%dest%"
tar -xf python.zip > nul 2>&1 || powershell -Command "Expand-Archive -Path 'python.zip' -DestinationPath '.' -Force" > nul
del python.zip /Q

:: Запуск программы
start "" /B ".\python\pythonw.exe" ".\Assistant.py"

endlocal
exit