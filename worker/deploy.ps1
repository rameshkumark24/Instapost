<#
.SYNOPSIS
  Deploy the Instapost publisher to Cloudflare and prove it works without posting.

.DESCRIPTION
  Go-live phase C in one command. It checks your tools, runs the publisher's
  own tests, signs you in to Cloudflare, deploys, stores the secrets the
  publisher reads (you type the ones only you know; the test key is
  generated), and then calls the publisher's test address once.

  The test address never posts, whatever today's cards say. It checks each
  account, today's cards, the holds and Telegram, and reports back. The script
  ends with "Test passed. Nothing was posted." and exit code 0 only when every
  check passed; any problem ends it with exit code 1.

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

$Config = Get-Content -Raw -Path "wrangler.toml"

function Get-Setting([string]$Name) {
    ([regex]::Match($Config, "(?m)^\s*$Name\s*=\s*""([^""]*)""")).Groups[1].Value
}

$Repo = Get-Setting "REPO"
# The accounts the publisher posts to. The secrets asked for and the accounts
# tested both follow this list, so an account added there cannot be missed here.
$Channels = @((Get-Setting "CHANNELS") -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ })
$Labels = @{ news = "news"; flirt = "tech-metaphor" }

function Write-Step([int]$Number, [string]$Title) {
    Write-Host ""
    Write-Host "$Number. $Title" -ForegroundColor Cyan
}

function Write-Ok([string]$Message) { Write-Host "   [ok]   $Message" -ForegroundColor Green }

function Write-Warn([string]$Message) { Write-Host "   [warn] $Message" -ForegroundColor Yellow }

function Write-Fail([string]$Message) { Write-Host "   [FAIL] $Message" -ForegroundColor Red }

function Stop-WithFix([string]$Message, [string]$Fix) {
    Write-Fail $Message
    if ($Fix) { Write-Host "          -> $Fix" }
    exit 1
}

# wrangler is pinned in package.json and installed beside this script, so every
# run uses the version this script was written against, not whatever is newest.
function Invoke-Wrangler {
    if ($MyInvocation.ExpectingInput) { $input | & npx --no wrangler @args }
    else { & npx --no wrangler @args }
}

# Runs a command with everything it prints captured as plain text.
function Invoke-Captured([scriptblock]$Command) {
    $text = & $Command 2>&1 | ForEach-Object { "$_" } | Out-String
    [pscustomobject]@{ Code = $LASTEXITCODE; Text = $text }
}

# --- 1 ----------------------------------------------------------------------
Write-Step 1 "Tools"
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Stop-WithFix "Node.js is not installed." "Install the LTS version from https://nodejs.org, open a new terminal, and run this again."
}
$NodeVersion = "$(node --version)".Trim()
if ([int]($NodeVersion -replace '^v(\d+).*$', '$1') -lt 22) {
    Stop-WithFix "Node.js $NodeVersion is too old; wrangler needs version 22 or newer." "Install the LTS version from https://nodejs.org, open a new terminal, and run this again."
}
if (-not $Repo -or -not $Channels) {
    Stop-WithFix "REPO or CHANNELS is missing from worker\wrangler.toml." "Send this output to Claude."
}
$Pinned = (Get-Content -Raw -Path "package.json" | ConvertFrom-Json).devDependencies.wrangler
$Installed = $null
if (Test-Path "node_modules/wrangler/package.json") {
    $Installed = (Get-Content -Raw -Path "node_modules/wrangler/package.json" | ConvertFrom-Json).version
}
if ($Installed -ne $Pinned) {
    Write-Host "   Installing wrangler $Pinned. The first time takes a minute or two."
    $install = Invoke-Captured { & npm ci --no-audit --no-fund }
    if ($install.Code -ne 0) {
        Write-Host $install.Text
        Stop-WithFix "could not install wrangler $Pinned." "Check your internet connection and run this again."
    }
}
Write-Ok "Node $NodeVersion, wrangler $Pinned, repository $Repo, accounts: $($Channels -join ', ')"

# --- 2 ----------------------------------------------------------------------
Write-Step 2 "Publisher tests"
$tests = Invoke-Captured { & node --test }
if ($tests.Code -ne 0) {
    Write-Host $tests.Text
    Stop-WithFix "the publisher's own tests failed, so nothing was deployed." "Send the output above to Claude."
}
Write-Ok "all publisher tests pass"

# --- 3 ----------------------------------------------------------------------
Write-Step 3 "Cloudflare sign-in"
$who = Invoke-Captured { Invoke-Wrangler whoami }
if ($who.Code -ne 0 -or $who.Text -match "not authenticated") {
    Write-Host "   A browser window opens. Sign in to Cloudflare and allow access, then come back here."
    Invoke-Wrangler login
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
$deploy = Invoke-Captured { Invoke-Wrangler deploy }
if ($deploy.Code -ne 0 -and $deploy.Text -match "workers\.dev subdomain") {
    # A new Cloudflare account has no workers.dev address yet. Wrangler offers
    # to register one only when it can ask, and captured output cannot ask, so
    # this one deploy runs in the open. The next is captured to read the address.
    Write-Host "   Your Cloudflare account needs a workers.dev address first, and wrangler asks for one now."
    Write-Host "   Answer yes, then choose any name."
    Invoke-Wrangler deploy
    if ($LASTEXITCODE -ne 0) {
        Stop-WithFix "the deploy failed." "Send the output above to Claude."
    }
    $deploy = Invoke-Captured { Invoke-Wrangler deploy }
}
Write-Host $deploy.Text
if ($deploy.Code -ne 0) {
    Stop-WithFix "the deploy failed." "Send the output above to Claude."
}
$Url = [regex]::Match($deploy.Text, "https://[A-Za-z0-9.-]+\.workers\.dev").Value
if (-not $Url) {
    Stop-WithFix "deployed, but Cloudflare printed no workers.dev address." "In the Cloudflare dashboard open Workers, choose instapost-publisher, enable its workers.dev route, then run this again with -SkipSecrets."
}
if ($deploy.Text -notmatch "15 14 \* \* \*") {
    Write-Warn "the 19:45 IST schedule (15 14 * * *) was not listed; check Triggers for instapost-publisher in the Cloudflare dashboard"
}
Write-Ok "deployed at $Url"

# --- 5 ----------------------------------------------------------------------
Write-Step 5 "Secrets"
$secrets = [ordered]@{ "IG_TOKEN" = "the Meta system-user token (go-live step A5)" }
foreach ($channel in $Channels) {
    $label = if ($Labels[$channel]) { $Labels[$channel] } else { $channel }
    $secrets["IG_USER_ID_$($channel.ToUpper())"] = "the $label account's Instagram ID, printed by the Gate A assistant"
}
$secrets["TG_TOKEN"] = "your Telegram bot token, the same value as the GitHub secret"
$secrets["TG_CHAT"] = "your Telegram chat ID, the same value as the GitHub secret"

if ($SkipSecrets) {
    Write-Warn "keeping the secrets already stored in Cloudflare"
}
else {
    foreach ($name in $secrets.Keys) {
        Write-Host "   $name is $($secrets[$name]). Wrangler asks for it next; what you type stays hidden."
        Invoke-Wrangler secret put $name
        if ($LASTEXITCODE -ne 0) {
            Stop-WithFix "could not store $name." "Run this again. The code is deployed but posts nothing until every secret is stored."
        }
        Write-Ok "$name stored"
    }
}

# Only for the test call below. Generated so you never have to invent or keep
# one; running this script again simply replaces it.
$ManualKey = [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
$ManualKey | Invoke-Wrangler secret put MANUAL_KEY | Out-Null
if ($LASTEXITCODE -ne 0) {
    Stop-WithFix "could not store the test key." "Run this again with -SkipSecrets."
}
Write-Ok "MANUAL_KEY generated and stored"

# --- 6 ----------------------------------------------------------------------
# The publisher's test address posts nothing whatever the cards say, so this
# needs no check of its own that could fail open: the guarantee is enforced
# where the posting happens.
Write-Step 6 "Test without posting"
Write-Host "   Calling the publisher's test address. It checks each account, today's cards and Telegram, and never posts."
Start-Sleep -Seconds 5
try {
    $result = Invoke-RestMethod -Uri "$Url/run" -Headers @{ "x-key" = $ManualKey } -TimeoutSec 180 -ErrorAction Stop
}
catch {
    Stop-WithFix "the publisher did not answer: $($_.Exception.Message)" "Wait a minute for the deployment to spread, then run this again with -SkipSecrets."
}
if ($result.mode -ne "test" -or $result.posted -ne $false) {
    Stop-WithFix "the publisher did not confirm that it ran as a test." "Send this output to Claude before running anything else."
}

$problems = 0
foreach ($channel in $Channels) {
    $outcome = $result.channels.$channel
    if ($null -eq $outcome) {
        Write-Fail "$($channel): the publisher returned no result for this account"
        $problems++
        continue
    }
    if ($outcome.error) {
        Write-Fail "$($channel): $($outcome.error)"
        $problems++
    }
    else {
        Write-Ok "$($channel): $($outcome.account) reachable"
    }
    Write-Host "          tonight: $($outcome.tonight)"
}
if ($result.telegram -eq "sent") {
    Write-Ok "Telegram: test message sent"
}
else {
    Write-Fail "Telegram: $($result.telegram)"
    $problems++
}
foreach ($note in @($result.notes)) {
    if ($note) { Write-Warn $note }
}

Write-Host ""
if ($problems) {
    Write-Host "Test failed: $problems problem(s) above. Nothing was posted." -ForegroundColor Red
    Write-Host "A wrong or missing secret: run this again without -SkipSecrets and type it again. Anything from Meta: send this output to Claude."
    exit 1
}
Write-Host "Test passed. Nothing was posted." -ForegroundColor Green
Write-Host "The publisher now runs every day at 19:45 IST. Nothing posts until the repository variable LIVE is set to true (go-live phase G)."
exit 0
