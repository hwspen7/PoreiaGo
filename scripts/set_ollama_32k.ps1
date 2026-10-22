# Apply 32K Ollama context and, optionally, start the local Ollama server.
# Only English text is used in source files and messages.
param(
    [switch]$Serve
)

$ErrorActionPreference = 'Stop'

$settings = @{
    'OLLAMA_CONTEXT_LENGTH' = '32768'
    'OLLAMA_MAX_LOADED_MODELS' = '1'
    'OLLAMA_NUM_PARALLEL' = '1'
}
foreach ($entry in $settings.GetEnumerator()) {
    [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, 'User')
    Set-Item -Path "Env:$($entry.Key)" -Value $entry.Value
    Write-Host "$($entry.Key)=$($entry.Value)"
}

# Use the previously configured model storage path when starting Ollama.
# In particular, avoid inadvertently using the default C: location.
foreach ($name in @('OLLAMA_MODELS', 'OLLAMA_NO_CLOUD')) {
    $configuredValue = [Environment]::GetEnvironmentVariable($name, 'User')
    if (-not [string]::IsNullOrWhiteSpace($configuredValue)) {
        Set-Item -Path "Env:$name" -Value $configuredValue
        Write-Host "$name=$configuredValue"
    }
}

if ($Serve) {
    $listener = Get-NetTCPConnection -LocalPort 11434 -State Listen -ErrorAction SilentlyContinue
    if ($listener) {
        throw 'Port 11434 is already in use. Quit Ollama in the Windows system tray before using -Serve.'
    }
    $ollama = Get-Command ollama -ErrorAction Stop
    Write-Host 'Starting Ollama with 32768 context tokens. Keep this window open.'
    & $ollama.Source serve
} else {
    Write-Host 'Configuration saved for the current Windows user.'
    Write-Host 'Quit and restart Ollama, or rerun with -Serve after quitting the tray app.'
    Write-Host 'After loading a model, verify the CONTEXT column with: ollama ps'
}
