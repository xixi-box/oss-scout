$pipe = New-Object System.IO.Pipes.NamedPipeClientStream('.', 'verge-mihomo-sidecar-release-ce5a94928fb90475150b01178a3cb6616adae55bb45bb556b4f5f22fbe17fa66', [System.IO.Pipes.PipeDirection]::InOut)
$pipe.Connect(3000)
$body = '{"path": "C:\Users\wangshun\AppData\Roaming\io.github.clash-verge-rev.clash-verge-rev\clash-verge.yaml"}'
$req = "PUT /configs?force=true HTTP/1.1`r`nHost: mihomo`r`nContent-Type: application/json`r`nContent-Length: $($body.Length)`r`nConnection: close`r`n`r`n$body"
$w = New-Object System.IO.StreamWriter($pipe); $w.AutoFlush = $true
$w.Write($req)
Start-Sleep -Milliseconds 1500
try { $resp = (New-Object System.IO.StreamReader($pipe)).ReadToEnd() } catch { $resp = '(read interrupted)' }
$line = ($resp -split "`r`n")[0]
Write-Output "reload response: $line"
$pipe.Dispose()
