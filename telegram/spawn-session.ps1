# Spawn a DEDICATED session in its own window and force it to the FOREGROUND so Principal sees it.
# Usage:  powershell -File spawn-session.ps1 -Bat "<path to .bat>" -Match "Digital Twin"
# The .bat should `title <something containing $Match>` and run the agent (e.g. `claude "<task>"`).
param(
  [Parameter(Mandatory=$true)][string]$Bat,
  [string]$Match = "Digital Twin"
)
Add-Type @"
using System; using System.Text; using System.Runtime.InteropServices;
public class Win {
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc f, IntPtr l);
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
}
"@
$p = Start-Process -FilePath "cmd.exe" -ArgumentList '/k', "`"$Bat`"" -PassThru
Start-Sleep -Seconds 2
$script:t = [IntPtr]::Zero
$cb = [Win+EnumProc]{ param($h,$l)
  if ([Win]::IsWindowVisible($h)) {
    $sb = New-Object System.Text.StringBuilder 256
    [Win]::GetWindowText($h,$sb,256) | Out-Null
    if ($sb.ToString() -like "*$Match*") { $script:t = $h; return $false }
  }; return $true }
[Win]::EnumWindows($cb, [IntPtr]::Zero) | Out-Null
if ($script:t -ne [IntPtr]::Zero) {
  [Win]::ShowWindow($script:t, 6) | Out-Null; Start-Sleep -Milliseconds 300   # minimize
  [Win]::ShowWindow($script:t, 3) | Out-Null                                  # restore maximized -> front
  [Win]::SetForegroundWindow($script:t) | Out-Null
  Write-Output "FOREGROUND ok (pid $($p.Id))"
} else { Write-Output "spawned (pid $($p.Id)) but window not found to foreground" }
