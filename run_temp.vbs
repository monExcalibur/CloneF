Dim fso, scriptPath, batPath, shell
Set fso = CreateObject("Scripting.FileSystemObject")
scriptPath = fso.GetParentFolderName(WScript.ScriptFullName)
batPath = scriptPath & "\Start.bat"

Set shell = CreateObject("Wscript.Shell")
shell.Run batPath, 0, False