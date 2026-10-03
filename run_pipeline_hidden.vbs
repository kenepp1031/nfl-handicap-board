' Launches run_pipeline.ps1 with no console window, for the scheduled task.
'
' Registering the task S4U (which would run it in session 0, invisibly) needs
' an elevated shell, so it runs as an interactive logon instead -- and
' powershell.exe is a console app, so "-WindowStyle Hidden" still flashes a
' window every hour. wscript is windowless, so launching through it doesn't.
'
' Run(..., 0, True) means hidden and wait; its return value is PowerShell's
' exit code, passed on so Task Scheduler's "Last Run Result" stays meaningful.
Dim shell, fso, scriptDir, command
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & _
          fso.BuildPath(scriptDir, "run_pipeline.ps1") & """"

WScript.Quit shell.Run(command, 0, True)
