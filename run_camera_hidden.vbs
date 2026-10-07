Option Explicit
' Lanza el supervisor del detector (camera_watchdog.py) sin mostrar ventana.
' Usa el Python disponible en el PATH (pythonw.exe). Toda la ruta se calcula
' a partir de la ubicacion de este archivo, asi que no hay rutas personales.
Dim sh, fso, base, command, result
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = base
command = Chr(34) & "pythonw.exe" & Chr(34) & " " & Chr(34) & base & "\camera_watchdog.py" & Chr(34)
' La tarea programada conserva este proceso padre: si el watchdog termina,
' el Programador de tareas aplica RestartOnFailure. La ventana queda oculta.
result = sh.Run(command, 0, True)
WScript.Quit result
