@echo off
chcp 65001 > nul
title Puente Camara - Acceso Remoto
cls
echo ========================================================
echo   INICIANDO PUENTE DE CAMARA
echo   Sirviendo el stream RTSP local como video HTTP.
echo   Configura la URL RTSP en puente_camara_puertomontt.py
echo   o con la variable de entorno CAMARA_RTSP.
echo ========================================================
echo.

cd /d "%~dp0"
python puente_camara_puertomontt.py

pause
