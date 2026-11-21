@echo off
if not exist "%~dp0cookie.txt" type nul > "%~dp0cookie.txt"
start "" notepad "%~dp0cookie.txt"
