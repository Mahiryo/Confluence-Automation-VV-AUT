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
    -Uri "https://default9744600e3e04492ebaa125ec245c6f.10.environment.api.powerplatform.com:443/powerautomate/automations/direct/cu/27/workflows/7c17c89066404835913d12ed16952415/triggers/manual/paths/invoke?api-version=1&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0&sig=SZ-zyKRtXTVS__7Y70BMj3YBJfMEn4b1WPluRBXkBCc" `
    -Method Post `
    -ContentType "application/json" `
    -Body $body