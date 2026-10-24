param([string]$Method, [string]$Path, [string]$Body)
$pipe = New-Object System.IO.Pipes.NamedPipeClientStream('.', 'verge-mihomo-sidecar-release-ce5a94928fb90475150b01178a3cb6616adae55bb45bb556b4f5f22fbe17fa66', [System.IO.Pipes.PipeDirection]::InOut)
$pipe.Connect(3000)
$req = "$Method $Path HTTP/1.1`r`nHost: mihomo`r`nContent-Type: application/json`r`nContent-Length: $(($Body | ForEach-Object { $_.Length }).Length)`r`nConnection: close`r`n`r`n$Body"
$w = New-Object System.IO.StreamWriter($pipe); $w.AutoFlush = $true
$w.Write($req)
Start-Sleep -Milliseconds 1200
try { $resp = (New-Object System.IO.StreamReader($pipe)).ReadToEnd() } catch { $resp = '' }
$pipe.Dispose()
$json = ($resp -split "`r`n`r`n", 2)[1]
Write-Output $json
