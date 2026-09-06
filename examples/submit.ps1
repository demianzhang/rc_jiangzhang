param(
    [string]$ApiBase = "http://127.0.0.1:8000",
    [string]$Destination = "http://127.0.0.1:9000/flaky",
    [string]$EventId = "registration-1001"
)

$ErrorActionPreference = "Stop"
if (-not $env:NOTIFY_API_KEY) {
    throw "Set NOTIFY_API_KEY before running this example."
}

$headers = @{
    "X-API-Key" = $env:NOTIFY_API_KEY
    "Idempotency-Key" = $EventId
}
$body = @{
    url = $Destination
    method = "POST"
    headers = @{
        "Content-Type" = "application/json"
        "Idempotency-Key" = $EventId
    }
    body = (@{ event = "user.registered"; user_id = 1001 } | ConvertTo-Json -Compress)
} | ConvertTo-Json -Depth 4 -Compress

$accepted = Invoke-RestMethod -Uri "$ApiBase/v1/notifications" -Method Post `
    -Headers $headers -ContentType "application/json" -Body $body
$accepted | ConvertTo-Json
for ($i = 0; $i -lt 90; $i++) {
    $notification = Invoke-RestMethod -Uri "$ApiBase/v1/notifications/$($accepted.id)" `
        -Headers $headers
    if ($notification.status -eq "succeeded" -or $notification.status -eq "dead") {
        $notification | ConvertTo-Json -Depth 5
        if ($notification.status -eq "dead") {
            throw "Notification is dead. Inspect history before redriving."
        }
        return
    }
    Start-Sleep -Seconds 1
}
throw "Notification is still queued or retrying; query it again later."
