# Preserve existing local secrets while updating previous V3.1 retrieval defaults.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $root '.env'
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $root '.env.example') $envFile
    Write-Host 'Created .env from .env.example.'
} else {
    Copy-Item $envFile "$envFile.bak" -Force
    Write-Host 'Backed up existing .env to .env.bak.'
}
$text = Get-Content $envFile -Raw
$settings = @{
    'POREIAGO_EMBEDDING_MODEL' = 'intfloat/multilingual-e5-large-instruct'
    'POREIAGO_RERANKER_MODEL' = 'mixedbread-ai/mxbai-rerank-base-v2'
    'POREIAGO_RETRIEVAL_MODE' = 'hybrid'
}
foreach ($key in $settings.Keys) {
    $regex = '(?m)^' + [regex]::Escape($key) + '=.*$'
    $line = "$key=$($settings[$key])"
    if ([regex]::IsMatch($text, $regex)) {
        $text = [regex]::Replace($text, $regex, $line)
    } else {
        $text = $text.TrimEnd() + [Environment]::NewLine + $line + [Environment]::NewLine
    }
}
[System.IO.File]::WriteAllText($envFile, $text, (New-Object System.Text.UTF8Encoding($false)))
Write-Host 'Updated local retrieval model settings. Restart FastAPI to apply the changes.'
