# Install ops-core as a Windows service via NSSM (Windows on-premise).
# waitress (pure-Python WSGI) serves app.main; NSSM keeps it running and starts it at boot.
# Prereqs: Python venv at C:\ops-core\.venv with requirements installed; NSSM on PATH.
# After install, verify it starts at boot by REBOOTING the machine (design doc 7.3).

$OpsDir = "C:\ops-core"

nssm install OpsCore "$OpsDir\.venv\Scripts\python.exe" "-m app.main"
nssm set OpsCore AppDirectory   $OpsDir
nssm set OpsCore AppStdout      "$OpsDir\logs\stdout.log"
nssm set OpsCore AppStderr      "$OpsDir\logs\stderr.log"
nssm set OpsCore AppRotateFiles 1
nssm set OpsCore Start          SERVICE_AUTO_START
nssm set OpsCore AppExit Default Restart
nssm start OpsCore

Write-Host "OpsCore installed. Reboot to confirm it starts automatically."
