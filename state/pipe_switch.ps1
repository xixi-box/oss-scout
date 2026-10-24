for ($i=1; $i -le 3; $i++) {
  try {
    $pipe = New-Object System.IO.Pipes.NamedPipeClientStream('.', 'verge-mihomo-sidecar-release-ce5a94928fb90475150b01178a3cb6616adae55bb45bb556b4f5f22fbe17fa66', [System.IO.Pipes.PipeDirection]::InOut)
    $pipe.Connect(5000)
    $body = '{"name": "自动选择"}'
    $req = "PUT /proxies/%E8%89%AF%E5%BF%83%E4%BA%91 HTTP/1.1`r`nHost: mihomo`r`nContent-Type: application/json`r`nContent-Length: $($body.Length)`r`nConnection: close`r`n`r`n$body"
    $w = New-Object System.IO.StreamWriter($pipe); $w.AutoFlush = $true
    $w.Write($req)
    Start-Sleep -Milliseconds 2500
    try { $resp = (New-Object System.IO.StreamReader($pipe)).ReadToEnd() } catch { $resp = '' }
    $pipe.Dispose()
    $line = ($resp -split "`r`n")[0]
    if ($line -match ' 2\d\d ') { Write-Output "attempt ${i}: OK ($line)"; break }
    else { Write-Output "attempt ${i}: $line" }
  } catch { Write-Output "attempt ${i}: $($_.Exception.Message)" }
  Start-Sleep -Seconds 2
}
