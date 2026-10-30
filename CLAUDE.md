# Regras deste projeto

- O Matheus roda comandos no **PowerShell do Windows**. Comando entregue a ele vai em sintaxe PowerShell
  (`$env:VAR = "x"`, não `VAR=x cmd`), começa com `cd` para a pasta do projeto e, se precisar de segredo do `.env`,
  usa um script que carrega o `.env` (ex.: `scripts/live_eval.ps1`). Motivo: em 30/10/2026 os comandos do eval
  ao vivo foram entregues em sintaxe bash e falharam no PowerShell dele, a partir da pasta errada.
