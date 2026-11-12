param(
  [switch]$Sqlite,
  [switch]$Down,
  [switch]$Logs
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if ($Down) {
  if ($Sqlite) { docker compose -f docker-compose.sqlite.yml down }
  else { docker compose down }
  exit $LASTEXITCODE
}

if ($Logs) {
  if ($Sqlite) { docker compose -f docker-compose.sqlite.yml logs -f app }
  else { docker compose logs -f app }
  exit $LASTEXITCODE
}

if ($Sqlite) {
  docker compose -f docker-compose.sqlite.yml up --build -d
} else {
  docker compose up --build -d
}

if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host "CheckStock запущен: http://localhost:8000"
