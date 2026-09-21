' Run the Hermes watchdog without creating a console window. Resolve the repo
' from this file so the scheduled task remains valid when the workspace moves.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
repoRoot = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
watchdog = fso.BuildPath(repoRoot, "scripts\hermes-watchdog.ps1")
logPath = fso.BuildPath(repoRoot, "_runtime\watchdog\hermes-watchdog.log")
quote = Chr(34)
command = "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File " & quote & watchdog & quote & " -LogPath " & quote & logPath & quote
sh.Run command, 0, False
