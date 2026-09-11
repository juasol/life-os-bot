' start.bat をコンソールウィンドウを表示せずに起動するためのランチャー。
' スタートアップフォルダにはこのファイルのショートカットを置く（start.bat 本体ではない）。
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
shell.Run """" & folder & "\start.bat""", 0, False
