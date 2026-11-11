' Vanta IDE - launch with no console window at all.
' Double-click this, or point a shortcut at it. Uses pythonw.exe from the
' project venv so the server, terminal and AI worker inherit a console-free
' parent; every child they spawn is created with CREATE_NO_WINDOW.
Option Explicit

Dim fso, shell, here, pyw, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

here = fso.GetParentFolderName(WScript.ScriptFullName)
pyw = here & "\.venv\Scripts\pythonw.exe"

If Not fso.FileExists(pyw) Then
  pyw = here & "\..\airllm\.venv\Scripts\pythonw.exe"
End If
If Not fso.FileExists(pyw) Then
  pyw = "pythonw.exe"
End If

cmd = """" & pyw & """ """ & here & "\desktop.py"""

' 0 = hidden window, False = do not wait for the process to finish.
shell.Run cmd, 0, False
