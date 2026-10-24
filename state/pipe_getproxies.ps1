for ($i=1; $i -le 3; $i++) {
  try {
    $pipe = New-Object System.IO.Pipes.NamedPipeClientStream('.', 'verge-mihomo-sidecar-release-ce5a94928fb90475150b01178a3cb6616adae55bb45bb556b4f5f22fbe17fa66', [System.IO.Pipes.PipeDirection]::InOut)
    $pipe.Connect(5000)
    $req = "GET /proxies HTTP/1.1`r`nHost: mihomo`r`nConnection: close`r`n`r`n"
    $w = New-Object System.IO.StreamWriter($pipe); $w.AutoFlush = $true
    $w.Write($req)
    Start-Sleep -Milliseconds 2500
    $sb = New-Object System.Text.StringBuilder
    $r = New-Object System.IO.StreamReader($pipe)
    try { while ($true) { $chunk = $r.Read(); if ($chunk -lt 0) { break }; [void]$sb.Append([char]$chunk) } } catch {}
    $pipe.Dispose()
    $resp = $sb.ToString()
    $line = ($resp -split "`r`n")[0]
    Write-Output "attempt ${i}: $line"
    $json = ($resp -split "`r`n`r`n", 2)[1]
    if ($json) { [System.IO.File]::WriteAllText('C:\Users\wangshun\AppData\Local\Temp\mihomo_proxies.json', $json) ; Write-Output "saved" ; break }
  } catch { Write-Output "attempt ${i}: $($_.Exception.Message)" }
  Start-Sleep -Seconds 2
}
