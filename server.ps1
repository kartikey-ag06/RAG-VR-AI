# Default: the Meta VR Glasses app (IWSDK build). Old Quest/extension version: run with -App web
param([string]$App = "glasses")
# server.ps1 - tiny static web server (no install needed) for the VR_RAG WebXR app.
# WebXR only works on https:// or http://localhost, so we serve web\ on http://localhost:8765
$port = 8765
$root = if ($App -eq "web") { Join-Path $PSScriptRoot "web" } else { Join-Path $PSScriptRoot "glasses_app\dist" }
$types = @{ ".html"="text/html; charset=utf-8"; ".js"="text/javascript; charset=utf-8"; ".json"="application/json";
            ".jpg"="image/jpeg"; ".png"="image/png"; ".css"="text/css"; ".ico"="image/x-icon";
            ".mp3"="audio/mpeg"; ".wasm"="application/wasm"; ".svg"="image/svg+xml"; ".woff2"="font/woff2";
            ".gltf"="model/gltf+json"; ".glb"="model/gltf-binary"; ".bin"="application/octet-stream"; ".uikitml"="text/plain; charset=utf-8"; ".md"="text/plain" }
# ---------------- Cloud LLM proxy: Grok (xAI) or Groq ----------------
# The browser calls /api/grok on THIS server; only this script ever sees the API key.
# Key: env XAI_API_KEY / GROQ_API_KEY, or the first line of grok_api_key.txt next to this script.
#   xai-...  -> Grok  (https://api.x.ai/v1)
#   gsk_...  -> Groq  (https://api.groq.com/openai/v1)
# Model: first line of grok_model.txt (optional). Otherwise a sensible default is picked.
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$script:autoModel = $null
function Get-GrokKey {
  if ($env:XAI_API_KEY) { return $env:XAI_API_KEY.Trim() }
  if ($env:GROQ_API_KEY) { return $env:GROQ_API_KEY.Trim() }
  $f = Join-Path $PSScriptRoot "grok_api_key.txt"
  if (Test-Path $f) { $k = (Get-Content $f -TotalCount 1); if ($k -and $k.Trim() -match '^(xai-|gsk_)') { return $k.Trim() } }
  return $null
}
function Get-Provider {
  $k = Get-GrokKey
  if (-not $k) { return $null }
  if ($k.StartsWith("gsk_")) { return @{ name = "groq"; base = "https://api.groq.com/openai/v1" } }
  return @{ name = "grok"; base = "https://api.x.ai/v1" }
}
function Get-GrokModel {
  $f = Join-Path $PSScriptRoot "grok_model.txt"
  if (Test-Path $f) { $m = (Get-Content $f -TotalCount 1); if ($m -and $m.Trim()) { return $m.Trim() } }
  $p = Get-Provider
  if (-not $p) { return "" }
  if ($p.name -eq "grok") { return "grok-4.7" }
  # Groq: ask which models this key can use and pick a good one (cached)
  if ($script:autoModel) { return $script:autoModel }
  $prefer = @("llama-3.3-70b-versatile", "openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.1-8b-instant")
  try {
    $r = Invoke-RestMethod -Uri "$($p.base)/models" -Headers @{ Authorization = "Bearer $(Get-GrokKey)" } -TimeoutSec 20
    $ids = @($r.data | ForEach-Object { $_.id })
    $pick = $prefer | Where-Object { $ids -contains $_ } | Select-Object -First 1
    if (-not $pick) { $pick = $ids | Where-Object { $_ -notmatch 'whisper|tts|guard|embed' } | Select-Object -First 1 }
    $script:autoModel = $pick
    return $pick
  } catch { return "llama-3.3-70b-versatile" }
}
# ---------------- Voice: speech-to-text via Groq Whisper ----------------
# Browser records audio -> POST /api/stt (raw audio bytes) -> this script -> Groq /audio/transcriptions.
Add-Type -AssemblyName System.Net.Http
$script:sttModel = $null
function Get-SttModel {
  if ($script:sttModel) { return $script:sttModel }
  $p = Get-Provider
  try {
    $r = Invoke-RestMethod -Uri "$($p.base)/models" -Headers @{ Authorization = "Bearer $(Get-GrokKey)" } -TimeoutSec 20
    $ids = @($r.data | ForEach-Object { $_.id })
    $pick = @("whisper-large-v3-turbo", "whisper-large-v3") | Where-Object { $ids -contains $_ } | Select-Object -First 1
    if (-not $pick) { $pick = $ids | Where-Object { $_ -match 'whisper' } | Select-Object -First 1 }
    if ($pick) { $script:sttModel = $pick; return $pick }
  } catch {}
  return "whisper-large-v3-turbo"
}
function Invoke-Stt($ctx) {
  $p = Get-Provider
  if (-not $p -or $p.name -ne "groq") { return @{ code = 400; json = '{"error":"Voice transcription needs a Groq key (gsk_...)"}' } }
  $ms = New-Object IO.MemoryStream
  $ctx.Request.InputStream.CopyTo($ms)
  $bytes = $ms.ToArray()
  if ($bytes.Length -lt 800) { return @{ code = 400; json = '{"error":"Recording too short - hold the button a bit longer"}' } }
  $ct = $ctx.Request.ContentType; if (-not $ct) { $ct = "audio/webm" }
  $mime = ($ct -split ';')[0].Trim()
  $ext = if ($mime -match 'ogg') { 'ogg' } elseif ($mime -match 'mp4|m4a') { 'm4a' } elseif ($mime -match 'wav') { 'wav' } elseif ($mime -match 'mpeg|mp3') { 'mp3' } else { 'webm' }
  $client = New-Object System.Net.Http.HttpClient
  $client.Timeout = [TimeSpan]::FromSeconds(60)
  $client.DefaultRequestHeaders.Authorization = New-Object System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", (Get-GrokKey))
  $form = New-Object System.Net.Http.MultipartFormDataContent
  $file = New-Object System.Net.Http.ByteArrayContent(,$bytes)
  $file.Headers.ContentType = [System.Net.Http.Headers.MediaTypeHeaderValue]::Parse($mime)
  $form.Add($file, "file", "speech.$ext")
  $form.Add((New-Object System.Net.Http.StringContent((Get-SttModel))), "model")
  $form.Add((New-Object System.Net.Http.StringContent("json")), "response_format")
  $form.Add((New-Object System.Net.Http.StringContent("0")), "temperature")
  try {
    $resp = $client.PostAsync("$($p.base)/audio/transcriptions", $form).GetAwaiter().GetResult()
    $text = $resp.Content.ReadAsStringAsync().GetAwaiter().GetResult()
    if (-not $resp.IsSuccessStatusCode) {
      Write-Host "STT error: HTTP $([int]$resp.StatusCode) $text" -ForegroundColor Yellow
      return @{ code = 502; json = (@{ error = "groq STT: HTTP $([int]$resp.StatusCode) - $text" } | ConvertTo-Json -Compress) }
    }
    return @{ code = 200; json = $text }
  } catch {
    $m = $_.Exception.InnerException; if (-not $m) { $m = $_.Exception }
    Write-Host "STT error: $($m.Message)" -ForegroundColor Yellow
    return @{ code = 502; json = (@{ error = "groq STT: $($m.Message)" } | ConvertTo-Json -Compress) }
  } finally { $client.Dispose() }
}
function Send-Json($res, [int]$code, [string]$json) {
  $bytes = [Text.Encoding]::UTF8.GetBytes($json)
  $res.StatusCode = $code; $res.ContentType = "application/json; charset=utf-8"
  $res.ContentLength64 = $bytes.Length; $res.OutputStream.Write($bytes, 0, $bytes.Length)
}
function Invoke-Grok($ctx) {
  $key = Get-GrokKey
  if (-not $key) { return @{ code = 400; json = '{"error":"No xAI key. Put it in grok_api_key.txt"}' } }
  $reader = New-Object IO.StreamReader($ctx.Request.InputStream, [Text.Encoding]::UTF8)
  $req = $reader.ReadToEnd() | ConvertFrom-Json
  $p = Get-Provider
  $body = @{ model = (Get-GrokModel); messages = $req.messages; temperature = 0.2; max_tokens = 600 } | ConvertTo-Json -Depth 10
  try {
    $r = Invoke-WebRequest -UseBasicParsing -Method Post -Uri "$($p.base)/chat/completions" `
      -Headers @{ Authorization = "Bearer $key" } -ContentType "application/json; charset=utf-8" `
      -Body ([Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 90
    $text = [Text.Encoding]::UTF8.GetString($r.RawContentStream.ToArray())
    return @{ code = 200; json = $text }
  } catch {
    $msg = $_.Exception.Message
    $code = ""
    if ($_.Exception.Response) { try { $code = [int]$_.Exception.Response.StatusCode } catch {} }
    if ($_.ErrorDetails -and $_.ErrorDetails.Message) { $msg = $_.ErrorDetails.Message }
    elseif ($_.Exception.Response) {
      try { $sr = New-Object IO.StreamReader($_.Exception.Response.GetResponseStream()); $b = $sr.ReadToEnd(); if ($b) { $msg = $b } } catch {}
    }
    $msg = "HTTP $code - $msg"
    Write-Host "Grok error: $msg" -ForegroundColor Yellow
    return @{ code = 502; json = (@{ error = "$($p.name) API: $msg" } | ConvertTo-Json -Compress) }
  }
}

# try 8765, and if it is busy (e.g. an older VR_RAG window is still open) use the next free port
$listener = $null
foreach ($p in $port..($port + 9)) {
  $l = New-Object System.Net.HttpListener
  $l.Prefixes.Add("http://localhost:$p/")
  try { $l.Start(); $listener = $l; $port = $p; break } catch { $l.Close() }
}
if (-not $listener) { Write-Host "No free port between 8765 and 8774" -ForegroundColor Red; Read-Host "Press Enter"; exit 1 }
Write-Host "VR_RAG ($App) running at http://localhost:$port/  (close this window to stop)" -ForegroundColor Green
if (Get-GrokKey) { Write-Host "AI: ON - $((Get-Provider).name) (model $(Get-GrokModel))" -ForegroundColor Cyan } else { Write-Host "AI: OFF - put an xAI (xai-...) or Groq (gsk_...) key in grok_api_key.txt" -ForegroundColor Yellow }
try { Start-Process "http://localhost:$port/" } catch {}
while ($listener.IsListening) {
  $ctx = $listener.GetContext()
  try {
  $rel = [Uri]::UnescapeDataString($ctx.Request.Url.AbsolutePath.TrimStart('/'))
  if ($rel -eq "api/grok/status") {
    $k = Get-GrokKey
    $prov = Get-Provider
    $pn = $(if ($prov) { $prov.name } else { "" })
    Send-Json $ctx.Response 200 (@{ configured = [bool]$k; provider = $pn; model = (Get-GrokModel); stt = ($pn -eq "groq") } | ConvertTo-Json -Compress)
    $ctx.Response.OutputStream.Close(); continue
  }
  if ($rel -eq "api/grok/models") {
    $key = Get-GrokKey
    try {
      $r = Invoke-WebRequest -UseBasicParsing -Uri "$((Get-Provider).base)/models" -Headers @{ Authorization = "Bearer $key" } -TimeoutSec 30
      Send-Json $ctx.Response 200 ([Text.Encoding]::UTF8.GetString($r.RawContentStream.ToArray()))
    } catch {
      $m = if ($_.ErrorDetails -and $_.ErrorDetails.Message) { $_.ErrorDetails.Message } else { $_.Exception.Message }
      Send-Json $ctx.Response 502 (@{ error = $m } | ConvertTo-Json -Compress)
    }
    $ctx.Response.OutputStream.Close(); continue
  }
  if ($rel -eq "api/stt" -and $ctx.Request.HttpMethod -eq "POST") {
    Write-Host "POST /api/stt -> groq ($(Get-SttModel))" -ForegroundColor Cyan
    $out = Invoke-Stt $ctx
    Send-Json $ctx.Response $out.code $out.json
    $ctx.Response.OutputStream.Close(); continue
  }
  if ($rel -eq "api/server/stop" -and $ctx.Request.HttpMethod -eq "POST") {
    Send-Json $ctx.Response 200 '{"stopping":true}'; $ctx.Response.OutputStream.Close()
    Write-Host "Stop requested - shutting down." -ForegroundColor Yellow; $listener.Stop(); break
  }
  if ($rel -eq "api/grok" -and $ctx.Request.HttpMethod -eq "POST") {
    Write-Host "POST /api/grok -> $((Get-Provider).name) ($(Get-GrokModel))" -ForegroundColor Cyan
    $out = Invoke-Grok $ctx
    Send-Json $ctx.Response $out.code $out.json
    $ctx.Response.OutputStream.Close(); continue
  }
  if ($rel -eq "") { $rel = "index.html" }
  $path = [IO.Path]::GetFullPath((Join-Path $root $rel))
  $res = $ctx.Response
  if ($path.StartsWith($root) -and (Test-Path $path -PathType Leaf)) {
    $bytes = [IO.File]::ReadAllBytes($path)
    $ext = [IO.Path]::GetExtension($path).ToLower()
    $res.ContentType = if ($types.ContainsKey($ext)) { $types[$ext] } else { "application/octet-stream" }
    $res.ContentLength64 = $bytes.Length
    $res.OutputStream.Write($bytes, 0, $bytes.Length)
  } else { $res.StatusCode = 404 }
  $res.OutputStream.Close()
  Write-Host "$($ctx.Request.HttpMethod) /$rel $($res.StatusCode)"
  } catch { Write-Host "request error: $($_.Exception.Message)" -ForegroundColor Yellow }
}
