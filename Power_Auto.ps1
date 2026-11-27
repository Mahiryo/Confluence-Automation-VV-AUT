$body = @{
    type = "message"
    attachments = @(
        @{
            contentType = "application/vnd.microsoft.teams.card"
            content = @{
                '$schema' = "http://adaptivecards.io/schemas/adaptive-card.json"
                type = "AdaptiveCard"
                version = "1.0"
                body = @(
                    @{
                        type = "TextBlock"
                        text = "Mensagem Teste v4 - Notificação teams por Webhook "
                        wrap = $true
                    }
                )
            }
        }
    )
} | ConvertTo-Json -Depth 10

Invoke-RestMethod `
    -Uri "" `
    -Method Post `
    -ContentType "application/json" `
    -Body $body
