<#
.SYNOPSIS
  Deploy the Instapost publisher to Cloudflare and prove it works without posting.

.DESCRIPTION
  Go-live phase C in one command. It checks your tools, runs the publisher's
  own tests, signs you in to Cloudflare, deploys, stores the secrets the
  publisher reads (you type the five only you know; the test key is
  generated), and then calls the publisher once.

  That test call refuses to run if any card on GitHub is set to really
  publish, so testing can never post anything.

  Run from the repo folder:
    powershell -ExecutionPolicy Bypass -File worker\deploy.ps1

.PARAMETER SkipSecrets
  Deploy code changes only and keep the secrets already stored in Cloudflare.
  The test key is regenerated either way.
#>
[CmdletBinding()]
param([switch]$SkipSecrets)

# Native tools such as npx write progress to stderr. Under "Stop", Windows
# PowerShell 5.1 turns those harmless lines into terminating errors, so exit
# codes are checked explicitly instead.
$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot

$Channels = @("news", "flirt")
$Repo = ([regex]::Match((Get-Content -Raw -Path "wrangler.toml"), 'REPO\s*=\s*"([^"]+)"')).Groups[1].Value

function Write-Step([int]$Number, [string]$Title) {
    Write-Host ""
    Write-Host "$Number. $Title" -ForegroundColor Cyan
}

function Write-Ok([string]$Message) { Write-Host "   [ok]   $Message" -ForegroundColor Green }

function Write-Warn([string]$Message) { Write-Host "   [warn] $Message" -ForegroundColor Yellow }

function Stop-WithFix([string]$Message, [string]$Fix) {
    Write-Host "   [FAIL] $Message" -ForegroundColor Red
    if ($Fix) { Write-Host "          -> $Fix" }
    exit 1
}

# --- 1 ----------------------------------------------------------------------
Write-Step 1 "Tools"
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Stop-WithFix "Node.js is not installed." "Install the LTS version from https://nodejs.org, open a new terminal, and run this again."
}
if (-not $Repo) {
    Stop-WithFix "REPO is missing from worker\wrangler.toml." "Send this output to Claude."
}
Write-Ok "Node $(node --version), repository $Repo"

# --- 2 ----------------------------------------------------------------------
Write-Step 2 "Publisher tests"
$testOutput = & node --test | Out-String
if ($LASTEXITCODE -ne 0) {
    Write-Host $testOutput
    Stop-WithFix "the publisher's own tests failed, so nothing was deployed." "Send the output above to Claude."
}
Write-Ok "all publisher tests pass"

# --- 3 ----------------------------------------------------------------------
Write-Step 3 "Cloudflare sign-in"
$who = & npx --yes wrangler whoami | Out-String
if ($LASTEXITCODE -ne 0 -or $who -match "not authenticated") {
    Write-Host "   A browser window opens. Sign in to Cloudflare and allow access, then come back here."
    & npx --yes wrangler login
    if ($LASTEXITCODE -ne 0) {
        Stop-WithFix "Cloudflare sign-in did not complete." "Run this again and finish signing in in the browser."
    }
}
Write-Ok "signed in to Cloudflare"

# --- 4 ----------------------------------------------------------------------
# Deployed before the secrets are stored: storing a secret for a Worker that
# does not exist yet makes wrangler ask a yes/no question, which would swallow
# a piped value. Until the secrets land, the 19:45 run has no account IDs and
# posts nothing.
Write-Step 4 "Deploy"
$deployOutput = & npx --yes wrangler deploy | Out-String
Write-Host $deployOutput
if ($LASTEXITCODE -ne 0) {
    Stop-WithFix "the deploy failed." "Send the output above to Claude."
}
$Url = [regex]::Match($deployOutput, "https://[A-Za-z0-9.-]+\.workers\.dev").Value
if (-not $Url) {
    Stop-WithFix "deployed, but Cloudflare printed no workers.dev address." "In the Cloudflare dashboard open Workers, choose instapost-publisher, enable its workers.dev route, then run this again with -SkipSecrets."
}
if ($deployOutput -notmatch "15 14 \* \* \*") {
    Write-Warn "the 19:45 IST schedule (15 14 * * *) was not listed; check Triggers for instapost-publisher in the Cloudflare dashboard"
}
Write-Ok "deployed at $Url"

# --- 5 ----------------------------------------------------------------------
Write-Step 5 "Secrets"
$secrets = [ordered]@{
    "IG_TOKEN"         = "the Meta system-user token (go-live step A5)"
    "IG_USER_ID_NEWS"  = "the news account's Instagram ID, printed by the Gate A assistant"
    "IG_USER_ID_FLIRT" = "the tech-metaphor account's Instagram ID, printed by the Gate A assistant"
    "TG_TOKEN"         = "your Telegram bot token, the same value as the GitHub secret"
    "TG_CHAT"          = "your Telegram chat ID, the same value as the GitHub secret"
}
if ($SkipSecrets) {
    Write-Warn "keeping the secrets already stored in Cloudflare"
}
else {
    foreach ($name in $secrets.Keys) {
        Write-Host "   $name is $($secrets[$name]). Wrangler asks for it next; what you type stays hidden."
        & npx --yes wrangler secret put $name
        if ($LASTEXITCODE -ne 0) {
            Stop-WithFix "could not store $name." "Run this again. The code is deployed but posts nothing until every secret is stored."
        }
        Write-Ok "$name stored"
    }
}

# Only for the test call below. Generated so you never have to invent or keep
# one; running this script again simply replaces it.
$ManualKey = [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
$ManualKey | & npx --yes wrangler secret put MANUAL_KEY | Out-Null
if ($LASTEXITCODE -ne 0) {
    Stop-WithFix "could not store the test key." "Run this again with -SkipSecrets."
}
Write-Ok "MANUAL_KEY generated and stored"

# --- 6 ----------------------------------------------------------------------
Write-Step 6 "Test without posting"
foreach ($channel in $Channels) {
    try {
        $card = Invoke-RestMethod -Uri "https://raw.githubusercontent.com/$Repo/main/dist/$channel/post.json" -TimeoutSec 30 -ErrorAction Stop
    }
    catch {
        $card = $null
    }
    if ($card -and -not $card.skip -and -not $card.dry_run) {
        Stop-WithFix "today's $($channel) card is set to really publish, so calling the publisher now would post it." "Deployment is complete. Skip this test, or delete the LIVE variable and wait for tomorrow's build first."
    }
}
Write-Host "   Calling the publisher once. It will also message you on Telegram with what it decided."
Start-Sleep -Seconds 5
try {
    $result = Invoke-RestMethod -Uri "$Url/run" -Headers @{ "x-key" = $ManualKey } -TimeoutSec 180 -ErrorAction Stop
}
catch {
    Stop-WithFix "the publisher did not answer: $($_.Exception.Message)" "Wait a minute for the deployment to spread, then run this again with -SkipSecrets."
}
foreach ($channel in $Channels) {
    $outcome = $result.$channel
    if ($null -eq $outcome) { Write-Warn "$($channel): no result returned"; continue }
    if ($outcome.published) { Write-Warn "$($channel): PUBLISHED $($outcome.published). A test should never do this; send this output to Claude." }
    elseif ($outcome.error) { Write-Warn "$($channel): $($outcome.error)" }
    else { Write-Ok "$($channel): $($outcome.skipped) - nothing posted" }
}

Write-Host ""
Write-Host "Done. The publisher now runs every day at 19:45 IST." -ForegroundColor Green
Write-Host "Nothing posts until the repository variable LIVE is set to true (go-live phase G)."
