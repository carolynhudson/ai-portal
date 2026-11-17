$cutoff = (Get-Date).AddDays(-1.5)
Write-Output "== kit files touched since $(Get-Date -Format 'yyyy-MM-dd'):"
Get-ChildItem C:\forge\kit -Recurse -File | Where-Object { $_.LastWriteTime -gt $cutoff } |
    Sort-Object LastWriteTime -Descending |
    ForEach-Object { Write-Output ($_.LastWriteTime.ToString("MM-dd HH:mm") + "  " + $_.FullName) }
Write-Output "== ComfyUI tree top-level files touched:"
Get-ChildItem C:\forge\ComfyUI -File | Where-Object { $_.LastWriteTime -gt $cutoff } |
    ForEach-Object { Write-Output ($_.LastWriteTime.ToString("MM-dd HH:mm") + "  " + $_.Name) }
Write-Output "== ComfyUI custom_nodes changed:"
Get-ChildItem C:\forge\ComfyUI\custom_nodes -Recurse -Depth 1 -File |
    Where-Object { $_.LastWriteTime -gt $cutoff } |
    Select-Object -First 20 |
    ForEach-Object { Write-Output ($_.LastWriteTime.ToString("MM-dd HH:mm") + "  " + $_.FullName) }
Write-Output "== engine log tails: newest lines mentioning qwen/update/new version:"
Get-ChildItem C:\forge\logs\comfyui-*.log -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1 |
    ForEach-Object { Write-Output ("newest log: " + $_.Name) }
