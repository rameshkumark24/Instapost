# Runs a copy of worker/deploy.ps1 against stand-ins for node, npm, wrangler
# and the publisher's test address, so tests can judge what the script does and
# how it exits without Cloudflare, Meta or a network. Driven by environment
# variables that tests/test_pipeline.py sets:
#
#   HARNESS_SCRIPT   the copy of deploy.ps1 to run
#   HARNESS_LOG      file that receives one line per command the script ran
#   HARNESS_ARGS     "-SkipSecrets", or empty
#   HARNESS_TESTS    ok | fail                    the publisher's own tests
#   HARNESS_DEPLOY   ok | fail | needs-subdomain  wrangler deploy
#   HARNESS_RUN      the JSON the test address answers, or "unreachable"

$ErrorActionPreference = "Continue"
$global:HarnessDeploys = 0

function global:Write-HarnessLog([string]$Line) {
    Add-Content -Path $env:HARNESS_LOG -Value $Line
}

function global:node {
    Write-HarnessLog ("node " + ($args -join " "))
    $global:LASTEXITCODE = 0
    if ($args -contains "--version") { "v24.11.1"; return }
    if ($env:HARNESS_TESTS -eq "fail") { "not ok 1 - a publisher test"; $global:LASTEXITCODE = 1; return }
    "ok - 24 tests"
}

function global:npm {
    Write-HarnessLog ("npm " + ($args -join " "))
    $global:LASTEXITCODE = 0
}

function global:npx {
    $line = "npx " + ($args -join " ")
    if ($MyInvocation.ExpectingInput) { $null = @($input); $line += " <piped>" }
    Write-HarnessLog $line
    $global:LASTEXITCODE = 0

    $words = @($args | Where-Object { $_ -notlike "--*" })     # wrangler <command> ...
    if ($words[1] -eq "whoami") { "You are logged in with an OAuth Token." }
    if ($words[1] -eq "deploy") {
        $global:HarnessDeploys++
        if ($env:HARNESS_DEPLOY -eq "fail") {
            "X [ERROR] Build failed"
            $global:LASTEXITCODE = 1
        }
        elseif ($env:HARNESS_DEPLOY -eq "needs-subdomain" -and $global:HarnessDeploys -eq 1) {
            Write-Error "You need to register a workers.dev subdomain before publishing to workers.dev"
            $global:LASTEXITCODE = 1
        }
        else {
            "Uploaded instapost-publisher (1.10 sec)"
            "Deployed instapost-publisher triggers (0.40 sec)"
            "  https://instapost-publisher.example.workers.dev"
            "  schedule: 15 14 * * *"
            "Current Version ID: 00000000-0000-0000-0000-000000000000"
        }
    }
}

function global:Invoke-RestMethod {
    [CmdletBinding()]
    param([string]$Uri, [hashtable]$Headers, [int]$TimeoutSec)
    Write-HarnessLog "GET $Uri"
    if ($env:HARNESS_RUN -eq "unreachable") { throw "Unable to connect to the remote server" }
    $env:HARNESS_RUN | ConvertFrom-Json
}

function global:Start-Sleep { }

if ($env:HARNESS_ARGS -eq "-SkipSecrets") { & $env:HARNESS_SCRIPT -SkipSecrets }
else { & $env:HARNESS_SCRIPT }
exit $LASTEXITCODE
