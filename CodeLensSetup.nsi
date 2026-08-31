; CodeLensSetup.nsi
;
; A thin NSIS bootstrap wrapper around the real installer (installer_app.py's
; PySide6 wizard, built to dist\CodeLens Setup\CodeLens Setup.exe).
;
; Why this exists: the previous single-file download was a raw PyInstaller
; --onefile exe, which self-extracts its whole payload to %TEMP% on every
; launch - a pattern SmartScreen/Defender's heuristics commonly flag. That
; was fixed by switching the real installer to a --onedir build (see
; installer_app.spec), but a --onedir build isn't a single downloadable
; file. NSIS's bootstrap stub is one of the most common installer wrappers
; on Windows - millions of unrelated legitimate apps share the identical
; stub code, which already carries baseline SmartScreen reputation that a
; low-prevalence raw PyInstaller bootloader doesn't. This script just
; silently unpacks the onedir build to a temp folder and launches it - all
; the actual install logic still lives in installer_app.py.

!define APP_NAME "CodeLens"
!define PAYLOAD_DIR "dist\CodeLens Setup"

Name "${APP_NAME} Setup"
OutFile "dist\CodeLens_Setup.exe"
Icon "Assets\app_icon.ico"
RequestExecutionLevel user
SilentInstall silent
Unicode true

Section "Install"
    InitPluginsDir
    SetOutPath "$PLUGINSDIR\payload"
    File /r "${PAYLOAD_DIR}\*.*"
    ExecWait '"$PLUGINSDIR\payload\CodeLens Setup.exe"'
SectionEnd
