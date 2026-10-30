# Eval ao vivo + juiz + README, no PowerShell do Windows.
# Uso (de qualquer pasta):  powershell -ExecutionPolicy Bypass -File C:\caminho\getnet-agents-v2\scripts\live_eval.ps1
# Lê ANTHROPIC_API_KEY do .env do projeto sem imprimir o valor. Custo medido fica em evals/results/live.json.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path ".env")) { throw "Falta o arquivo .env com ANTHROPIC_API_KEY em $root" }
Get-Content ".env" | Where-Object { $_ -match '^\s*[A-Za-z_][A-Za-z0-9_]*\s*=' } | ForEach-Object {
    $name, $value = $_ -split '=', 2
    Set-Item -Path "env:$($name.Trim())" -Value $value.Trim()
}
if (-not $env:ANTHROPIC_API_KEY) { throw "ANTHROPIC_API_KEY vazia no .env" }

$env:LLM_PROVIDER = "anthropic"
$env:PYTHONIOENCODING = "utf-8"
$py = Join-Path $root ".venv\Scripts\python.exe"

& $py -m evals.run_evals --out evals/results/live.json      # 1. eval ao vivo (Haiku + Sonnet)
& $py -m evals.judge evals/results/live.json                # 2. juiz Opus 5.5 + concordância humana
$env:LLM_PROVIDER = "offline"
& $py -m evals.run_evals | Out-Null                         # 3. relatório offline (fonte do bloco principal)
& $py scripts/fill_readme.py                                # 4. números no README
