@echo off
title VR_RAG server (Quest + extension version)
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0server.ps1" -App web
