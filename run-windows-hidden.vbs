Option Explicit

Dim shell, fileSystem, projectRoot, scriptPath, powershellPath, command, quote
Set shell = CreateObject("WScript.Shell")
Set fileSystem = CreateObject("Scripting.FileSystemObject")

projectRoot = fileSystem.GetParentFolderName(WScript.ScriptFullName)
scriptPath = fileSystem.BuildPath(projectRoot, "scripts\bot_tray.ps1")
powershellPath = shell.ExpandEnvironmentStrings("%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe")
quote = Chr(34)
command = quote & powershellPath & quote & " -NoLogo -NoProfile -Sta -ExecutionPolicy Bypass -WindowStyle Hidden -File " & quote & scriptPath & quote

shell.Run command, 0, False
