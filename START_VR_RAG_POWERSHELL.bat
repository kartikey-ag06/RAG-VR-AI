@echo off
title VR_RAG server (PowerShell, keyword search only)
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0server.ps1"
