Set fso = CreateObject("Scripting.FileSystemObject")
srcDir = fso.GetParentFolderName(WScript.ScriptFullName)
watcherPy = fso.BuildPath(srcDir, "watcher.py")
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run "pythonw.exe """ & watcherPy & """", 0, False
