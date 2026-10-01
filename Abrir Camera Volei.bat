@echo off
chcp 65001 >nul
title Câmera Vôlei
cd /d "%~dp0"

rem ---- acha um Python instalado
set "PY="
py -3.14 -c "import sys" >nul 2>&1 && set "PY=py -3.14"
if not defined PY py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys" >nul 2>&1 && set "PY=python"
if not defined PY (
  echo.
  echo  Python nao encontrado.
  echo  Instale o Python 3.14 em https://www.python.org/downloads/ marcando "Add Python to PATH"
  echo  e abra este arquivo de novo.
  echo.
  pause
  exit /b 1
)

rem ---- instala os componentes na primeira vez
%PY% -c "import PySide6, cv2, numpy, imageio_ffmpeg, onnxruntime, pandas, scipy, tqdm" >nul 2>&1
if errorlevel 1 (
  echo.
  echo  Preparando o programa pela primeira vez. Isso leva alguns minutos...
  echo.
  %PY% -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo  Nao consegui instalar os componentes. Confira a internet e tente de novo.
    pause
    exit /b 1
  )
)

rem ---- abre sem a janela preta
for /f "delims=" %%i in ('%PY% -c "import sys,os;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))"') do set "PYW=%%i"
if exist "%PYW%" (
  start "" "%PYW%" "%~dp0camera_volei_app.py"
) else (
  %PY% "%~dp0camera_volei_app.py"
)
