# Run once in an elevated PowerShell on the Windows host.
# Opens the dashboard and the app port range to the local network only.

New-NetFirewallRule -DisplayName "App Launcher dashboard" `
  -Direction Inbound -Action Allow -Protocol TCP -LocalPort 80 `
  -Profile Private

New-NetFirewallRule -DisplayName "App Launcher apps" `
  -Direction Inbound -Action Allow -Protocol TCP -LocalPort 20000-29999 `
  -Profile Private
