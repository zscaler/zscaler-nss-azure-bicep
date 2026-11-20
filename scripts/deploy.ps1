<#
.SYNOPSIS
Deploys the Zscaler NSS VM with one command and reports the progress of the deployment.

.DESCRIPTION
The Windows counterpart of scripts/deploy.sh and the Makefile. It uses the Azure CLI (az),
which must be installed and signed in (az login). It runs in Windows PowerShell 5.1 and in
PowerShell 7.

The resource group is used as it is when it exists and is created in -Location when it
does not.

Without -ResourceGroup, Preview and Deploy ask for the values in the console, and offer
the answers of the last run again. Watch, Outputs and Destroy then use the resource group
of those answers. With -ResourceGroup nothing is asked.

Everything is done with the Azure CLI, with two exceptions: for the question about the SSH
rule, a public service is asked for the IP address this computer has on the internet, and
Destroy deletes the NSS server object with scripts/delete-nss-server.py, which needs
Python 3.10 or later.

Secrets are never arguments of this script. The parameter file reads them from the
environment (NSS_SAS_TOKEN, NSS_VM_PASSWORD, ZSCALER_*, ZIA_*). This script only reports
which of them are set and never prints a value. One that is not set is asked for without
showing it when the values are asked, and is kept for this run only.

In a console the progress of the VHD copy is a bar that is redrawn in place. Set the
NO_COLOR environment variable to any value for a bar without colours.

.PARAMETER Action
Preview shows what a deployment would change and changes nothing. Deploy deploys
main.sub.bicep and reports progress until the deployment ends. Watch reports the progress
of a deployment that was started earlier. Outputs prints the outputs of the last deployment.
Destroy deletes what the deployment created in Azure, and the NSS server object it
registered in ZIA. It shows what is deleted and deletes nothing before that is confirmed.

.PARAMETER ResourceGroup
Resource group of the NSS VM. Defaults to the RESOURCE_GROUP environment variable. When it
is set, nothing is asked.

.PARAMETER Location
Azure region. Required for a new resource group. Defaults to the location of an existing
resource group.

.PARAMETER ParameterFile
Parameter file for main.sub.bicep. Default: main.sub.bicepparam in the repository.

.PARAMETER Parameters
Parameter values that replace those of the parameter file, as name=value pairs: several
strings, or one string with the pairs separated by spaces. A value cannot contain a space.
Defaults to the PARAMETERS environment variable.

.PARAMETER DeploymentName
Name of the deployment record. Default: nss-<ResourceGroup>.

.PARAMETER PollSeconds
Seconds between two progress checks.

.PARAMETER Yes
Deploys without the confirmation question. It does not apply to Destroy.

.PARAMETER AnswersFile
File the answers are saved in. Default: the ANSWERS_FILE environment variable, otherwise
.nss-deploy-answers in the repository. Secrets are never saved.

.PARAMETER PublicIpUrl
Web addresses that answer with the public IP address of the caller as text: several
strings, or one string with the addresses separated by spaces. The first answer is offered
for the SSH rule when the values are asked. Default: the PUBLIC_IP_URL environment
variable, otherwise https://api.ipify.org and https://checkip.amazonaws.com. Set to none
to ask no service.

.PARAMETER ConfirmDestroy
The name of the resource group, to destroy without the confirmation question. Defaults to
the CONFIRM_DESTROY environment variable.

.PARAMETER KeepNssServer
Destroy leaves the NSS server object in ZIA. Also set by KEEP_NSS_SERVER=1.

.PARAMETER KeepResourceGroup
Destroy keeps the resource group and deletes only the resources of the deployment in it.
Also set by KEEP_RESOURCE_GROUP=1.

.PARAMETER DeleteResourceGroup
Destroy deletes a resource group that holds nothing but the deployment, without the
question that is asked when the deployment did not create it. Also set by
KEEP_RESOURCE_GROUP=0. A resource group that holds anything else is kept.

.PARAMETER NssServerId
ID of the NSS server object Destroy deletes in ZIA. Default: the NSS_SERVER_ID environment
variable, otherwise the ID the deployment recorded.

.PARAMETER Python
Python 3.10 or later that runs scripts/delete-nss-server.py. Default: the PYTHON
environment variable, otherwise python, python3 or py. When it has no zscaler-sdk-python,
that package is installed into a temporary virtual environment that is removed afterwards.

.EXAMPLE
.\scripts\deploy.ps1 Deploy

.EXAMPLE
$env:NSS_SAS_TOKEN = '<sas-token>'
.\scripts\deploy.ps1 Deploy -ResourceGroup NSS-RG -Location eastus

.EXAMPLE
.\scripts\deploy.ps1 Preview -ResourceGroup NSS-RG -Location eastus -Parameters 'vhdRegion=znssprodeu', 'installNssCertificate=true'

.EXAMPLE
.\scripts\deploy.ps1 Watch -ResourceGroup NSS-RG

.EXAMPLE
.\scripts\deploy.ps1 Destroy -ResourceGroup NSS-RG
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('Help', 'Preview', 'Deploy', 'Watch', 'Outputs', 'Destroy')]
    [string] $Action = 'Help',

    [string] $ResourceGroup = $env:RESOURCE_GROUP,

    [string] $Location = $env:LOCATION,

    [string] $ParameterFile = $env:PARAMETER_FILE,

    [string[]] $Parameters = @(),

    [string] $DeploymentName = $env:DEPLOYMENT_NAME,

    [ValidateRange(0, 3600)]
    [int] $PollSeconds = 20,

    [switch] $Yes,

    [string] $AnswersFile = $env:ANSWERS_FILE,

    [string[]] $PublicIpUrl = @(),

    [string] $ConfirmDestroy = $env:CONFIRM_DESTROY,

    [switch] $KeepNssServer,

    [switch] $KeepResourceGroup,

    [switch] $DeleteResourceGroup,

    [string] $NssServerId = $env:NSS_SERVER_ID,

    [string] $Python = $env:PYTHON
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repository = Split-Path -Parent $PSScriptRoot
if (-not $Parameters -and $env:PARAMETERS) {
    $Parameters = @($env:PARAMETERS)
}
$Parameters = @($Parameters | ForEach-Object { $_ -split '\s+' } | Where-Object { $_ })
if (-not $ParameterFile) {
    $ParameterFile = Join-Path $repository 'main.sub.bicepparam'
}
if (-not $AnswersFile) {
    $AnswersFile = Join-Path $repository '.nss-deploy-answers'
}
$AnswersFile = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($AnswersFile)
if (-not $PublicIpUrl -and $env:PUBLIC_IP_URL) {
    $PublicIpUrl = @($env:PUBLIC_IP_URL)
}
if (-not $PublicIpUrl) {
    $PublicIpUrl = @('https://api.ipify.org', 'https://checkip.amazonaws.com')
}
$PublicIpUrl = @($PublicIpUrl | ForEach-Object { $_ -split '\s+' } | Where-Object { $_ })
if ($env:KEEP_NSS_SERVER -in @('1', 'y', 'yes', 'true')) {
    $KeepNssServer = $true
}
if ($env:KEEP_RESOURCE_GROUP -in @('1', 'y', 'yes', 'true')) {
    $KeepResourceGroup = $true
}
elseif ($env:KEEP_RESOURCE_GROUP -in @('0', 'n', 'no', 'false')) {
    $DeleteResourceGroup = $true
}

$secretVariables = @(
    'NSS_SAS_TOKEN', 'NSS_VM_PASSWORD',
    'ZSCALER_CLIENT_ID', 'ZSCALER_CLIENT_SECRET', 'ZSCALER_PRIVATE_KEY', 'ZSCALER_VANITY_DOMAIN', 'ZSCALER_CLOUD',
    'ZIA_USERNAME', 'ZIA_PASSWORD', 'ZIA_API_KEY', 'ZIA_CLOUD'
)
$finishedStates = @('Succeeded', 'Failed', 'Canceled')

$script:Subscription = ''
$script:TargetGroup = ''
$script:Watching = $false
$script:States = @{}
$script:Finished = @{}
$script:Failed = @{}
$script:LogLinesShown = @{}
$script:GroupMode = ''
$script:GroupLocation = ''
$script:GroupRead = ''

# The questions. Answers holds the values of this run, Saved those of the answers file.
$script:Questions = $false    # the values come from questions and from the answers file
$script:CanAsk = $false       # this is a console, so a question can be asked
$script:AcceptSaved = $false  # a saved answer that is still valid is taken without asking
$script:Asking = $false       # the questions are being asked
$script:Asked = $false        # at least one question was asked
$script:ApiKind = ''          # oneapi or legacy: the Zscaler API the credentials are asked for
# What a run that stops while it asks has not done.
$script:NothingDone = 'Nothing was deployed.'
# Destroy: what to say when Ctrl+C stops it after the confirmation.
$script:StopNote = ''
$script:Answers = @{}
$script:Saved = @{}
$script:Regions = @()

# The names that are saved, in the order of the questions. A name in capitals is a setting of
# this script or an environment variable; every other name is a parameter of main.sub.bicep.
$answerNames = @(
    'RESOURCE_GROUP', 'LOCATION', 'vmName', 'virtualMachineSize', 'vhdRegion', 'sshSourceAddressPrefix',
    'vNetNewOrExisting', 'NETWORK_DEFAULTS', 'virtualNetworkName', 'virtualNetworkResourceGroup', 'subnetName',
    'subnetName2', 'vNetAddressRange', 'subnetAddressRange', 'subnetAddressRange2', 'stgNewOrExisting',
    'nameStorageAccount', 'installNssCertificate', 'nssServerName', 'nssServerType', 'certificateSubnetName',
    'certificateSubnetAddressRange', 'ZSCALER_API', 'ZSCALER_VANITY_DOMAIN', 'ZSCALER_CLOUD', 'ZIA_USERNAME',
    'ZIA_CLOUD'
)

# The values to choose from, as main.bicep allows them: "value" or "value|description".
$vmSizeOptions = @(
    'Standard_D2_v4|2 vCPUs, 8 GB'
    'Standard_D4_v4|4 vCPUs, 16 GB'
    'Standard_D8_v4|8 vCPUs, 32 GB'
    'Standard_D16_v4|16 vCPUs, 64 GB'
)
$vhdRegionOptions = @(
    'znssprod|US Commercial'
    'znssprodeu|Europe'
    'znssprodau|Australia'
    'zsgovnsstexas|US Gov Texas'
    'zsgovnssarizona|US Gov Arizona'
    'zsgovnssiowa|US Gov Iowa'
    'zsgovnssvirginia|US Gov Virginia'
)
$serverTypeOptions = @(
    'SOFTWARE_AA_FLAG'
    'NSS_FOR_WEB'
    'NSS_FOR_FIREWALL'
    'VZEN'
    'VZEN_SME'
    'VZEN_SMLB'
    'PINNED_NSS'
    'MD5_CAPABLE'
    'ADP'
    'ZIRSVR'
    'NSS_FOR_ZPA'
)

# The copy script writes the progress of the VHD copy to its log in this form.
$progressPattern = '^VHD copy (Pending|Success): ([0-9][0-9,.]*) of ([0-9][0-9,.]*) bytes copied\.$'

# The progress bar of the VHD copy. In a console (Live) it is one line that is redrawn in
# place; every other line is printed above it.
$script:Live = $false
$script:BarActive = $false
$script:BarDrawn = 0
$script:BarOwner = ''
$script:BarHead = ''
$script:BarTail = ''
$script:BarLength = 0
$script:BarComplete = $false
$script:BarFrame = 0
$script:LastProgress = ''
$script:Style = @{
    Reset = ''; Empty = ''; Spin = ''; Text = ''; Done = ''; Gradient = @('')
    Full = '#'; Free = '-'; Mark = '*'; Spinner = @('|', '/', '-', '\')
}

function Get-LogoRgb {
    param([int] $R, [int] $G, [int] $B)
    return ([char]27 + "[38;2;${R};${G};${B}m")
}

function Test-LogoTruecolor {
    if ($env:NO_COLOR) {
        return $false
    }
    if ([Console]::IsOutputRedirected) {
        return $false
    }
    $colorTerm = $env:COLORTERM
    if ($colorTerm -match '^(?i:truecolor|24bit)$') {
        return $true
    }
    $term = $env:TERM
    if ($term -match '256color|kitty|iterm') {
        return $true
    }
    return $false
}

function Write-ZscalerLogo {
    $art = @(
        '███████╗███████╗ ██████╗ █████╗ ██╗     ███████╗██████╗ '
        '╚══███╔╝██╔════╝██╔════╝██╔══██╗██║     ██╔════╝██╔══██╗'
        '  ███╔╝ ███████╗██║     ███████║██║     █████╗  ██████╔╝'
        ' ███╔╝  ╚════██║██║     ██╔══██║██║     ██╔══╝  ██╔══██╗'
        '███████╗███████║╚██████╗██║  ██║███████╗███████╗██║  ██║'
        '╚══════╝╚══════╝ ╚═════╝╚═╝  ╚═╝╚══════╝╚══════╝╚═╝  ╚═╝'
    )
    $width = 0
    foreach ($line in $art) {
        if ($line.Length -gt $width) {
            $width = $line.Length
        }
    }
    $pad = 2
    $inner = $width + $pad * 2
    $reset = [char]27 + '[0m'

    if (-not (Test-LogoTruecolor)) {
        $bar = '-' * $inner
        Write-Host ''
        Write-Host "  +$bar+"
        foreach ($line in $art) {
            $padded = $line.PadRight($width)
            Write-Host ('  |' + (' ' * $pad) + $padded + (' ' * $pad) + '|')
        }
        Write-Host "  +$bar+"
        Write-Host ''
        return
    }

    $border = Get-LogoRgb -R 51 -G 85 -B 153
    $shadowColor = Get-LogoRgb -R 0 -G 61 -B 153
    $startR = 85; $startG = 204; $startB = 255
    $endR = 0; $endG = 61; $endB = 153
    $denom = [Math]::Max($width - 1, 1)
    $blank = (' ' * $width)
    $shadow = ('░' * $width)
    $hLine = ([char]0x2500) * $inner

    Write-Host ''
    Write-Host ("  ${border}$([char]0x256D)${hLine}$([char]0x256E)${reset}")
    Write-Host ("  ${border}$([char]0x2502)${reset}" + (' ' * $pad) + $blank + (' ' * $pad) + "${border}$([char]0x2502)${reset}")
    foreach ($line in $art) {
        $padded = $line.PadRight($width)
        $gradient = ''
        $lastColorKey = $null
        for ($i = 0; $i -lt $width; $i++) {
            $ch = $padded[$i]
            if ($ch -eq ' ') {
                $gradient += $ch
                continue
            }
            $t = $i / $denom
            $rr = [int]($startR + ($endR - $startR) * $t)
            $gg = [int]($startG + ($endG - $startG) * $t)
            $bb = [int]($startB + ($endB - $startB) * $t)
            $colorKey = "$rr,$gg,$bb"
            if ($colorKey -ne $lastColorKey) {
                $gradient += (Get-LogoRgb -R $rr -G $gg -B $bb)
                $lastColorKey = $colorKey
            }
            $gradient += $ch
        }
        $gradient += $reset
        Write-Host ("  ${border}$([char]0x2502)${reset}" + (' ' * $pad) + $gradient + (' ' * $pad) + "${border}$([char]0x2502)${reset}")
    }
    Write-Host ("  ${border}$([char]0x2502)${reset}" + (' ' * $pad) + "${shadowColor}${shadow}${reset}" + (' ' * $pad) + "${border}$([char]0x2502)${reset}")
    Write-Host ("  ${border}$([char]0x2502)${reset}" + (' ' * $pad) + $blank + (' ' * $pad) + "${border}$([char]0x2502)${reset}")
    Write-Host ("  ${border}$([char]0x2570)${hLine}$([char]0x256F)${reset}")
    Write-Host ''
}

# Removes the bar from the screen, so that a line can be printed in its place.
function Clear-Bar {
    if ($script:BarDrawn -gt 0) {
        [Console]::Write("`r" + (' ' * $script:BarDrawn) + "`r")
        $script:BarDrawn = 0
    }
}

# Draws the bar with the next position of the spinner.
function Write-Bar {
    if (-not $script:BarActive) {
        return
    }
    $spinner = $script:Style.Spinner
    $frame = $spinner[$script:BarFrame % $spinner.Count]
    $script:BarFrame++
    [Console]::Write("`r" + $script:BarHead + $script:Style.Spin + $frame + $script:Style.Reset + $script:BarTail)
    $script:BarDrawn = $script:BarLength
}

# Leaves the bar on the screen as a normal line and ends it.
function Complete-Bar {
    if (-not $script:BarActive) {
        return
    }
    Clear-Bar
    $mark = ' '
    if ($script:BarComplete) {
        $mark = $script:Style.Done + $script:Style.Mark + $script:Style.Reset
    }
    Write-Host ($script:BarHead + $mark + $script:BarTail)
    $script:BarActive = $false
    $script:BarOwner = ''
}

function Stop-Script {
    param([string] $Message)
    $script:Asking = $false
    $script:StopNote = ''
    Clear-Bar
    Write-Host "ERROR: $Message"
    exit 1
}

function Write-Step {
    param([string] $Text)
    Clear-Bar
    Write-Host ('[{0}] {1}' -f (Get-Date -Format 'HH:mm:ss'), $Text)
    Write-Bar
}

# Decides how the progress of the VHD copy is shown. A bar that is redrawn needs a console.
function Initialize-Display {
    $windows = $env:OS -eq 'Windows_NT'
    try {
        $script:Live = -not [Console]::IsOutputRedirected -and $env:TERM -ne 'dumb'
    }
    catch {
        $script:Live = $false
    }

    $escape = [string] [char] 27
    $colors = 0
    if ($script:Live -and -not $env:NO_COLOR -and (Get-Property $Host.UI 'SupportsVirtualTerminal')) {
        $colors = 8
        if ($windows -or "$env:TERM" -match '256color' -or $env:COLORTERM) {
            $colors = 256
        }
    }
    if ($colors -ge 8) {
        $script:Style.Reset = "${escape}[0m"
        $script:Style.Empty = "${escape}[2m"
        $script:Style.Spin = "${escape}[35m"
        $script:Style.Text = "${escape}[1m"
        $script:Style.Done = "${escape}[32m"
        $script:Style.Gradient = @("${escape}[36m")
    }
    if ($colors -ge 256) {
        $script:Style.Empty = "${escape}[38;5;240m"
        $script:Style.Spin = "${escape}[38;5;213m"
        $script:Style.Done = "${escape}[38;5;46m"
        $script:Style.Gradient = @(33, 39, 45, 51, 50, 49, 48, 47, 46 | ForEach-Object { "${escape}[38;5;${_}m" })
    }

    # The characters are written as numbers so that this file stays plain ASCII, which
    # Windows PowerShell 5.1 needs for a script without a byte order mark.
    if ($windows) {
        $unicode = [bool] $env:WT_SESSION -or [Console]::OutputEncoding.CodePage -eq 65001
    }
    else {
        $unicode = "$env:LC_ALL$env:LC_CTYPE$env:LANG" -match '(?i)utf-?8'
    }
    if ($unicode) {
        $script:Style.Full = [string] [char] 0x2588
        $script:Style.Free = [string] [char] 0x2591
        $script:Style.Mark = [string] [char] 0x2713
        $script:Style.Spinner = @(0x280B, 0x2819, 0x2839, 0x2838, 0x283C, 0x2834, 0x2826, 0x2827, 0x2807, 0x280F |
                ForEach-Object { [string] [char] $_ })
    }
}

# Waits until the next progress check and keeps the spinner of the bar turning.
function Wait-NextCheck {
    if (-not $script:BarActive) {
        Start-Sleep -Seconds $PollSeconds
        return
    }
    $end = (Get-Date).AddSeconds($PollSeconds)
    while ((Get-Date) -lt $end) {
        Write-Bar
        Start-Sleep -Milliseconds 100
    }
}

function Format-Gibibytes {
    param([long] $Bytes)
    $tenths = [long] [math]::Floor(([decimal] $Bytes * 10) / 1073741824)
    return '{0}.{1}' -f [long] [math]::Floor($tenths / 10), ($tenths % 10)
}

# Shows the progress of the VHD copy: in a console as a bar that is redrawn in place,
# otherwise as a line. The same progress is not shown twice.
function Show-Progress {
    param([string] $Name, [int] $Depth, [long] $Copied, [long] $Total)
    $percent = [int] [math]::Min(100, [math]::Floor(([decimal] $Copied * 100) / $Total))
    $text = '{0} of {1} GiB' -f (Format-Gibibytes $Copied), (Format-Gibibytes $Total)
    if ($script:LastProgress -eq "$Name|$percent|$text") {
        return
    }
    $script:LastProgress = "$Name|$percent|$text"
    $prefix = '{0}  | VHD copy' -f ('  ' * $Depth)

    $width = 0
    if ($script:Live) {
        $columns = 80
        try {
            if ([Console]::WindowWidth -gt 0) {
                $columns = [Console]::WindowWidth
            }
        }
        catch {
            $columns = 80
        }
        # Besides the bar the line holds the time (11), the spinner (3), the percentage (8),
        # the prefix and the text. One column stays free so that the line never wraps.
        $width = [math]::Min(30, $columns - 23 - $prefix.Length - $text.Length)
    }
    if ($width -lt 10) {
        Complete-Bar
        Write-Step ('{0}: {1}% ({2})' -f $prefix, $percent, $text)
        return
    }

    Clear-Bar
    $filled = [int] [math]::Floor($percent * $width / 100)
    $gradient = $script:Style.Gradient
    $cells = ''
    for ($cell = 0; $cell -lt $filled; $cell++) {
        $cells += $gradient[[int] [math]::Floor($cell * $gradient.Count / $width)] + $script:Style.Full
    }
    $cells += $script:Style.Reset + $script:Style.Empty + ($script:Style.Free * ($width - $filled))
    $script:BarHead = '[{0}] {1} ' -f (Get-Date -Format 'HH:mm:ss'), $prefix
    $script:BarTail = ' {0}{1}  {2}{3,3}%{1}  {4}' -f $cells, $script:Style.Reset, $script:Style.Text, $percent, $text
    $script:BarLength = $script:BarHead.Length + 1 + 1 + $width + 2 + 4 + 2 + $text.Length
    $script:BarComplete = $percent -ge 100
    $script:BarActive = $true
    $script:BarOwner = $Name
    Write-Bar
}

# Every call of the Azure CLI goes through here. Warnings are suppressed, the CLI gets no
# input, and a failing call does not end the script; the caller checks $LASTEXITCODE.
function Invoke-Az {
    param(
        [Parameter(Mandatory)] [string[]] $Arguments,
        [switch] $Quiet
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        if ($Quiet) {
            $null | & az @Arguments --only-show-errors 2>$null
        }
        else {
            $null | & az @Arguments --only-show-errors
        }
    }
    finally {
        $ErrorActionPreference = $previous
    }
}

function Get-AzJson {
    param([Parameter(Mandatory)] [string[]] $Arguments)
    $text = Invoke-Az -Arguments ($Arguments + @('--output', 'json')) -Quiet
    if ($LASTEXITCODE -ne 0 -or -not $text) {
        return $null
    }
    return (($text -join "`n") | ConvertFrom-Json)
}

# Reads a property that the Azure CLI may leave out, without failing in strict mode.
function Get-Property {
    param($Object, [string] $Name)
    if ($null -eq $Object) {
        return $null
    }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return $null
    }
    return $property.Value
}

function Test-Variable {
    param([string] $Name)
    return [bool] [Environment]::GetEnvironmentVariable($Name)
}

function Assert-AzureCli {
    if ($script:Subscription) {
        return
    }
    if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
        Stop-Script 'The Azure CLI (az) is not installed. See https://learn.microsoft.com/cli/azure/install-azure-cli'
    }
    $account = Get-AzJson -Arguments @('account', 'show')
    if (-not $account) {
        Stop-Script "Not signed in to Azure. Run 'az login' and, for another subscription, 'az account set --subscription <name or id>'."
    }
    $script:Subscription = '{0} / {1}' -f $account.name, $account.id
}

function Set-DeploymentName {
    if (-not $script:DeploymentName) {
        if (-not $ResourceGroup -and (Test-Path -LiteralPath $AnswersFile -PathType Leaf)) {
            Import-Answers
            if ($script:Saved.ContainsKey('RESOURCE_GROUP') -and $script:Saved['RESOURCE_GROUP'] -and
                -not (Get-GroupProblem $script:Saved['RESOURCE_GROUP'])) {
                $script:ResourceGroup = $script:Saved['RESOURCE_GROUP']
                Write-Host "Resource group '$ResourceGroup', from the answers in $AnswersFile."
            }
        }
        if (-not $ResourceGroup) {
            Stop-Script "Set -ResourceGroup, for example: .\scripts\deploy.ps1 $Action -ResourceGroup <resource-group-name>"
        }
        $name = "nss-$ResourceGroup"
        if ($name.Length -gt 64) {
            $name = $name.Substring(0, 64)
        }
        $script:DeploymentName = $name
    }
}

# ---------------------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------------------

# A name with a lower-case letter is a parameter of main.sub.bicep. A name in capitals is a
# setting of this script or an environment variable.
function Test-IsParameter {
    param([string] $Name)
    return $Name -cmatch '[a-z]'
}

# Returns the value -Parameters gives a parameter, and nothing when it gives none: only
# then the parameter is asked for.
function Get-GivenValue {
    param([string] $Name)
    $value = $null
    if (Test-IsParameter $Name) {
        foreach ($pair in $Parameters) {
            if ($pair.StartsWith("$Name=", [StringComparison]::Ordinal)) {
                $value = $pair.Substring($Name.Length + 1)
            }
        }
    }
    return $value
}

# Returns the value in use: the one -Parameters gives, otherwise the answer.
function Get-Current {
    param([string] $Name)
    $value = Get-GivenValue $Name
    if ($null -ne $value) {
        return $value
    }
    if ($script:Answers.ContainsKey($Name)) {
        return $script:Answers[$Name]
    }
    return ''
}

# Returns the value a parameter has in the parameter file, when it is written there as a
# text or as true or false. These values are the defaults of the questions.
function Get-FileValue {
    param([string] $Name)
    foreach ($line in @(Get-Content -LiteralPath $ParameterFile)) {
        if ($line -cmatch "^param\s+$Name\s*=\s*(?:'([^']*)'|(true|false))\s*(//.*)?$") {
            return "$($Matches[1])$($Matches[2])"
        }
    }
    return ''
}

function Set-Answer {
    param([string] $Name, [string] $Value)
    $script:Answers[$Name] = $Value
}

# Reads the answers file into Saved. Only names this script saves are read, and the file is
# never run as a script.
function Import-Answers {
    $script:Saved = @{}
    try {
        $lines = [System.IO.File]::ReadAllLines($AnswersFile)
    }
    catch {
        Stop-Script "The answers in '$AnswersFile' could not be read."
    }
    foreach ($line in $lines) {
        if (-not $line -or $line.StartsWith('#') -or $line -match '[\x00-\x1f\x7f]') {
            continue
        }
        $at = $line.IndexOf('=')
        if ($at -lt 1) {
            continue
        }
        $name = $line.Substring(0, $at)
        if ($answerNames -ccontains $name) {
            $script:Saved[$name] = $line.Substring($at + 1)
        }
    }
}

function Save-Answers {
    $lines = @(
        "# Answers to the questions of 'make deploy' and scripts/deploy.ps1, saved on $(Get-Date -Format 'yyyy-MM-dd HH:mm')."
        '# The next run offers them again. Secrets are never saved here. Delete this file to start over.'
    )
    foreach ($name in $answerNames) {
        if ($script:Answers.ContainsKey($name)) {
            $lines += "$name=$($script:Answers[$name])"
        }
    }
    try {
        # Without a byte order mark, so that scripts/deploy.sh reads the same file.
        [System.IO.File]::WriteAllLines($AnswersFile, [string[]] $lines, (New-Object System.Text.UTF8Encoding $false))
    }
    catch {
        Stop-Script "The answers could not be saved in '$AnswersFile'."
    }
    Write-Host "The answers are saved in $AnswersFile, without the secrets."
}

# Reads one line that was typed, without the spaces around it.
function Read-Reply {
    param([string] $Prompt)
    $reply = Read-Host $Prompt
    if ($null -eq $reply) {
        Write-Host ''
        Stop-Script "The input ended before every question was answered. $script:NothingDone"
    }
    return "$reply".Trim()
}

# Before a question: ends the run where nothing can be asked, and notes that the answers
# have to be saved again.
function Assert-CanAsk {
    param([string] $Name)
    if (-not $script:CanAsk) {
        Stop-Script "$Name is not answered in $AnswersFile, and this is not a console to ask it in. Run this in a console, or set -ResourceGroup and give the values in -Parameters."
    }
    $script:Asked = $true
}

# After answers that do not fit together: says why and asks them again.
function Request-Again {
    param([string] $Problem)
    if (-not $script:CanAsk) {
        Stop-Script "$Problem Correct the values in $AnswersFile, or run this in a console."
    }
    Write-Host "  $Problem"
    $script:AcceptSaved = $false
}

# Asks a question that is answered with yes or no. Returns true for yes. Default is the
# answer of the Enter key.
function Read-YesNo {
    param([string] $Question, [bool] $Default)
    $hint = 'y/N'
    if ($Default) {
        $hint = 'Y/n'
    }
    while ($true) {
        $reply = Read-Reply "$Question [$hint]"
        if (-not $reply) {
            return $Default
        }
        if ($reply -in @('y', 'yes')) {
            return $true
        }
        if ($reply -in @('n', 'no')) {
            return $false
        }
        Write-Host '  Answer yes or no.'
    }
}

# Asks for one value and keeps it. A value -Parameters gives is not asked for. Check is the
# name of a function that returns why a value cannot be used. Default is used when nothing
# is saved. An optional value may be empty.
function Read-Answer {
    param([string] $Name, [string] $Question, [string] $Check, [string] $Default = '', [switch] $Optional)
    if ($null -ne (Get-GivenValue $Name)) {
        return
    }
    if ($script:Answers.ContainsKey($Name)) {
        $Default = $script:Answers[$Name]
    }
    elseif ($script:Saved.ContainsKey($Name)) {
        $saved = $script:Saved[$Name]
        $problem = ''
        if ($saved) {
            $problem = & $Check $saved
        }
        if ((-not $saved -and $Optional) -or ($saved -and -not $problem)) {
            if ($script:AcceptSaved) {
                Set-Answer $Name $saved
                return
            }
            $Default = $saved
        }
        elseif ($problem) {
            Write-Host "  The saved value of $Name cannot be used. $problem"
        }
    }
    Assert-CanAsk $Name
    while ($true) {
        $shown = $Default
        if ($Optional -and $Default) {
            $shown = "$Default, - for none"
        }
        if ($shown) {
            $reply = Read-Reply "$Question [$shown]"
        }
        else {
            $reply = Read-Reply $Question
        }
        if (-not $reply) {
            $reply = $Default
        }
        elseif ($Optional -and $reply -eq '-') {
            $reply = ''
        }
        if (-not $reply) {
            if ($Optional) {
                break
            }
            Write-Host '  A value is needed.'
            continue
        }
        $problem = & $Check $reply
        if (-not $problem) {
            break
        }
        Write-Host "  $problem"
    }
    Set-Answer $Name $reply
}

# Asks to choose one of several values and keeps it. An option is "value" or "value|description".
function Read-Choice {
    param([string] $Name, [string] $Title, [string] $Default, [string[]] $Options)
    if ($null -ne (Get-GivenValue $Name)) {
        return
    }
    $values = @($Options | ForEach-Object { ($_ -split '\|', 2)[0] })
    $saved = $null
    if ($script:Answers.ContainsKey($Name)) {
        $saved = $script:Answers[$Name]
    }
    elseif ($script:Saved.ContainsKey($Name)) {
        $saved = $script:Saved[$Name]
    }
    if ($null -ne $saved -and $values -ccontains $saved) {
        if ($script:AcceptSaved) {
            Set-Answer $Name $saved
            return
        }
        $Default = $saved
    }
    Assert-CanAsk $Name
    $width = ($values | Measure-Object -Property Length -Maximum).Maximum
    Write-Host $Title
    $defaultIndex = 1
    for ($index = 0; $index -lt $Options.Count; $index++) {
        $value, $description = $Options[$index] -split '\|', 2
        if ($value -ceq $Default) {
            $defaultIndex = $index + 1
        }
        if ($description) {
            Write-Host ('  {0,2}) {1}  {2}' -f ($index + 1), $value.PadRight($width), $description)
        }
        else {
            Write-Host ('  {0,2}) {1}' -f ($index + 1), $value)
        }
    }
    while ($true) {
        $reply = Read-Reply "Choose 1-$($Options.Count) [$defaultIndex]"
        if (-not $reply) {
            $reply = "$defaultIndex"
        }
        $chosen = ''
        for ($index = 0; $index -lt $values.Count; $index++) {
            if ($reply -ceq "$($index + 1)" -or $reply -ceq $values[$index]) {
                $chosen = $values[$index]
            }
        }
        if ($chosen) {
            break
        }
        Write-Host "  Enter a number from 1 to $($Options.Count)."
    }
    Set-Answer $Name $chosen
}

# Asks a yes or no question and keeps the answer as true or false.
function Read-YesNoAnswer {
    param([string] $Name, [string] $Question, [string] $Default = 'false')
    if ($null -ne (Get-GivenValue $Name)) {
        return
    }
    $saved = $null
    if ($script:Answers.ContainsKey($Name)) {
        $saved = $script:Answers[$Name]
    }
    elseif ($script:Saved.ContainsKey($Name)) {
        $saved = $script:Saved[$Name]
    }
    if ($saved -cin @('true', 'false')) {
        if ($script:AcceptSaved) {
            Set-Answer $Name $saved
            return
        }
        $Default = $saved
    }
    Assert-CanAsk $Name
    if (Read-YesNo $Question ($Default -ceq 'true')) {
        Set-Answer $Name 'true'
    }
    else {
        Set-Answer $Name 'false'
    }
}

# Asks for a value the parameter file reads from an environment variable that is not a
# secret, and sets the variable for this run. A variable that is set is not asked for.
function Read-VariableAnswer {
    param([string] $Name, [string] $Question, [string] $Check, [switch] $Optional)
    if (Test-Variable $Name) {
        return
    }
    Read-Answer -Name $Name -Question $Question -Check $Check -Optional:$Optional
    if ($script:Answers.ContainsKey($Name) -and $script:Answers[$Name]) {
        [Environment]::SetEnvironmentVariable($Name, $script:Answers[$Name])
    }
}

# Asks for a secret without showing it and sets its environment variable for this run. It is
# never saved and never printed. A variable that is set is not asked for.
function Read-Secret {
    param([string] $Name, [string] $Question, [string] $Check = '')
    if (Test-Variable $Name) {
        return
    }
    if (-not $script:CanAsk) {
        Stop-Script "$Name is not set, and this is not a console to ask for it in. Set it first: `$env:$Name = '<value>'"
    }
    while ($true) {
        $secure = Read-Host -AsSecureString "$Question (not shown, not saved)"
        if ($null -eq $secure) {
            Write-Host ''
            Stop-Script "The input ended before every question was answered. $script:NothingDone"
        }
        $value = [System.Net.NetworkCredential]::new('', $secure).Password
        if (-not $value) {
            Write-Host '  A value is needed.'
            continue
        }
        if ($Check) {
            $problem = & $Check $value
            if ($problem) {
                Write-Host "  $problem"
                continue
            }
        }
        break
    }
    [Environment]::SetEnvironmentVariable($Name, $value)
}

# The functions below check a value. For a value that cannot be used they return why, and
# nothing for one that can. The patterns compare with case and name every letter and digit,
# because \w and \d also match letters and digits outside ASCII.

function Get-GroupProblem {
    param([string] $Value)
    if ($Value -cnotmatch '^[A-Za-z0-9_.()-]{1,90}$' -or $Value.EndsWith('.')) {
        return 'A resource group name has 1 to 90 letters, digits, hyphens, underscores, periods and parentheses, and does not end with a period.'
    }
}

function Get-LocationProblem {
    param([string] $Value)
    if ($Value -cnotmatch '^[a-z0-9]+$') {
        return 'A region is written in lower-case letters and digits, for example eastus.'
    }
    if ($script:Regions.Count -gt 0 -and $script:Regions -cnotcontains $Value) {
        return "'$Value' is not a region of this subscription. The regions are: $($script:Regions -join ' ')"
    }
}

function Get-VmNameProblem {
    param([string] $Value)
    if ($Value -cnotmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$' -or $Value -match '[.-]$') {
        return 'A VM name has 1 to 64 letters, digits, hyphens, underscores and periods, starts with a letter or digit and does not end with a hyphen or period.'
    }
}

function Get-NetworkNameProblem {
    param([string] $Value)
    if ($Value -cnotmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,62}[A-Za-z0-9_]$') {
        return 'A virtual network name has 2 to 64 letters, digits, hyphens, underscores and periods, starts with a letter or digit and ends with a letter, digit or underscore.'
    }
}

function Get-SubnetNameProblem {
    param([string] $Value)
    if ($Value -cnotmatch '^[A-Za-z0-9]([A-Za-z0-9._-]{0,78}[A-Za-z0-9_])?$') {
        return 'A subnet name has 1 to 80 letters, digits, hyphens, underscores and periods, starts with a letter or digit and ends with a letter, digit or underscore.'
    }
}

function Get-StorageNameProblem {
    param([string] $Value)
    if ($Value -cnotmatch '^[a-z0-9]{3,24}$') {
        return 'A storage account name has 3 to 24 lower-case letters and digits.'
    }
}

function Get-ServerNameProblem {
    param([string] $Value)
    if ($Value -cnotmatch '^[A-Za-z0-9][A-Za-z0-9 ._-]{0,254}$') {
        return 'Use letters, digits, spaces, hyphens, underscores and periods, starting with a letter or digit. Another name can be set in the parameter file.'
    }
}

function Get-DomainProblem {
    param([string] $Value)
    if ($Value -cnotmatch '^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$') {
        return 'Use letters, digits, hyphens and periods, for example acme.'
    }
}

function Get-UserNameProblem {
    param([string] $Value)
    if ($Value -cnotmatch '^\S{1,254}$') {
        return 'A user name has no spaces.'
    }
}

function Get-SasTokenProblem {
    param([string] $Value)
    if (-not $Value.Contains('sig=')) {
        return 'This is not a SAS token: it has no sig= part. Paste the token as Zscaler Support sent it.'
    }
}

# Returns the first and the last address of an IPv4 range a.b.c.d/n as two numbers, and
# nothing when the text is not such a range.
function Get-RangeNumbers {
    param([string] $Range)
    if ($Range -cnotmatch '^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})/([0-9]{1,2})$') {
        return $null
    }
    $parts = @(1..5 | ForEach-Object { [int] $Matches[$_] })
    if ($parts[4] -gt 32) {
        return $null
    }
    [long] $number = 0
    foreach ($part in $parts[0..3]) {
        if ($part -gt 255) {
            return $null
        }
        $number = $number * 256 + $part
    }
    return @($number, ($number + [long] [math]::Pow(2, 32 - $parts[4]) - 1))
}

function Format-Address {
    param([long] $Number)
    return '{0}.{1}.{2}.{3}' -f (($Number -shr 24) -band 255), (($Number -shr 16) -band 255),
        (($Number -shr 8) -band 255), ($Number -band 255)
}

function Get-RangeProblem {
    param([string] $Value)
    $numbers = Get-RangeNumbers $Value
    if ($null -eq $numbers) {
        return 'An address range is written as an IPv4 address and a prefix length, for example 192.168.100.0/24.'
    }
    $size = $numbers[1] - $numbers[0] + 1
    $extra = $numbers[0] % $size
    if ($extra -ne 0) {
        return "$Value does not start at the first address of a range. The range it is in is $(Format-Address ($numbers[0] - $extra))/$(($Value -split '/')[1])."
    }
}

function Get-SshSourceProblem {
    param([string] $Value)
    if ($Value -ceq '*') {
        return
    }
    if ($Value -cmatch '^[0-9.]+$') {
        if ($null -eq (Get-RangeNumbers "$Value/32")) {
            return 'This is not an IPv4 address. Enter an address such as 203.0.113.10, a range such as 203.0.113.0/24, or *.'
        }
        return
    }
    return Get-RangeProblem $Value
}

# Checks that subnets lie inside a virtual network and do not overlap. Returns why not.
function Get-LayoutProblem {
    param([string] $Network, [string[]] $Subnets)
    $outer = Get-RangeNumbers $Network
    if ($null -eq $outer) {
        return
    }
    $seen = @()
    foreach ($range in $Subnets) {
        $numbers = Get-RangeNumbers $range
        if ($null -eq $numbers) {
            continue
        }
        if ($numbers[0] -lt $outer[0] -or $numbers[1] -gt $outer[1]) {
            return "The subnet $range is not inside the virtual network $Network."
        }
        foreach ($other in $seen) {
            if ($numbers[0] -le $other.Last -and $numbers[1] -ge $other.First) {
                return "The subnets $($other.Range) and $range overlap."
            }
        }
        $seen += @{ Range = $range; First = $numbers[0]; Last = $numbers[1] }
    }
}

# Finds out once whether the resource group exists: GroupMode and GroupLocation.
function Read-Group {
    if ($script:GroupRead -ceq $ResourceGroup) {
        return
    }
    $exists = Invoke-Az -Arguments @('group', 'exists', '--name', $ResourceGroup, '--output', 'tsv')
    if ($LASTEXITCODE -ne 0) {
        Stop-Script "Could not check whether the resource group '$ResourceGroup' exists."
    }
    if ("$exists".Trim() -eq 'true') {
        $group = Get-AzJson -Arguments @('group', 'show', '--name', $ResourceGroup)
        if (-not $group) {
            Stop-Script "Could not read the resource group '$ResourceGroup'."
        }
        # A group that is being deleted still exists, and a deployment into it fails.
        if ((Get-Property (Get-Property $group 'properties') 'provisioningState') -eq 'Deleting') {
            Stop-Script "The resource group '$ResourceGroup' is being deleted. Run this again when 'az group exists --name $ResourceGroup' answers false."
        }
        $script:GroupMode = 'existing'
        $script:GroupLocation = $group.location
    }
    else {
        $script:GroupMode = 'new'
        $script:GroupLocation = ''
    }
    $script:GroupRead = $ResourceGroup
}

# Finds the IP address this computer has on the internet. Returns the address and the
# service that answered, and nothing when no service answers with an IPv4 address. Nothing
# but such an address is taken from an answer.
function Get-PublicAddress {
    if ($PublicIpUrl.Count -eq 1 -and $PublicIpUrl[0] -ceq 'none') {
        return $null
    }
    $previous = $ProgressPreference
    $ProgressPreference = 'SilentlyContinue'
    try {
        foreach ($url in $PublicIpUrl) {
            try {
                # Windows PowerShell 5.1 does not offer TLS 1.2 by itself on older systems.
                [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
                $reply = Invoke-RestMethod -Uri $url -TimeoutSec 5 -MaximumRedirection 0 -UseBasicParsing -ErrorAction Stop
            }
            catch {
                continue
            }
            if ($reply -isnot [string]) {
                continue
            }
            $text = $reply.Trim()
            if ($text -cnotmatch '^[0-9.]{7,15}$') {
                continue
            }
            $numbers = Get-RangeNumbers "$text/32"
            if ($null -ne $numbers) {
                return @{ Address = (Format-Address $numbers[0]); From = $url }
            }
        }
    }
    finally {
        $ProgressPreference = $previous
    }
    return $null
}

# Asks which addresses may reach the VM on SSH, and offers the public IP address of this
# computer. The address is only looked up when the question is asked.
function Read-SshSource {
    $offered = ''
    if ($script:CanAsk -and $null -eq (Get-GivenValue 'sshSourceAddressPrefix')) {
        $saved = ''
        if ($script:Saved.ContainsKey('sshSourceAddressPrefix')) {
            $saved = $script:Saved['sshSourceAddressPrefix']
        }
        if ($saved -and (Get-SshSourceProblem $saved)) {
            $saved = ''
        }
        if (-not $saved -or -not $script:AcceptSaved) {
            $found = Get-PublicAddress
            if ($found) {
                $offered = $found.Address
                if (-not $saved -or $saved -ceq $offered) {
                    Write-Host "  Your public IP address is $offered, as $($found.From) sees it. Enter allows SSH from this address only."
                }
                else {
                    Write-Host "  Your public IP address is $offered, as $($found.From) sees it. The answer of the last run is $saved."
                }
            }
        }
    }
    Read-Answer 'sshSourceAddressPrefix' 'IP address or range that may reach the VM on SSH, for example 203.0.113.10 or 203.0.113.0/24 (* is every address)' 'Get-SshSourceProblem' $offered
}

# Asks for the address ranges of a new virtual network until they fit together.
function Read-Ranges {
    while ($true) {
        Read-Answer 'vNetAddressRange' 'Address range of the virtual network' 'Get-RangeProblem' (Get-FileValue 'vNetAddressRange')
        Read-Answer 'subnetAddressRange' 'Address range of the service subnet' 'Get-RangeProblem' (Get-FileValue 'subnetAddressRange')
        Read-Answer 'subnetAddressRange2' 'Address range of the management subnet' 'Get-RangeProblem' (Get-FileValue 'subnetAddressRange2')
        $problem = Get-LayoutProblem (Get-Current 'vNetAddressRange') @(
            (Get-Current 'subnetAddressRange'), (Get-Current 'subnetAddressRange2'))
        if (-not $problem) {
            break
        }
        Request-Again $problem
    }
}

function Read-CertificateRange {
    while ($true) {
        Read-Answer 'certificateSubnetAddressRange' 'Address range of that subnet' 'Get-RangeProblem' (Get-FileValue 'certificateSubnetAddressRange')
        $problem = Get-LayoutProblem (Get-Current 'vNetAddressRange') @(
            (Get-Current 'subnetAddressRange'), (Get-Current 'subnetAddressRange2'),
            (Get-Current 'certificateSubnetAddressRange'))
        if (-not $problem) {
            break
        }
        Request-Again $problem
    }
}

# True when one set of Zscaler API variables is complete.
function Test-ZscalerCredentials {
    $oneApi = (Test-Variable 'ZSCALER_CLIENT_ID') -and (Test-Variable 'ZSCALER_VANITY_DOMAIN') -and
        ((Test-Variable 'ZSCALER_CLIENT_SECRET') -or (Test-Variable 'ZSCALER_PRIVATE_KEY'))
    $legacy = (Test-Variable 'ZIA_USERNAME') -and (Test-Variable 'ZIA_PASSWORD') -and
        (Test-Variable 'ZIA_API_KEY') -and (Test-Variable 'ZIA_CLOUD')
    return $oneApi -or $legacy
}

# Finds out which Zscaler API is used, from the variables that are set or with a question,
# and asks for its values that are not secrets: ApiKind. Purpose is what the credentials
# are for.
function Request-Api {
    param([string] $Purpose)
    if (Test-Variable 'ZSCALER_CLIENT_ID') {
        $script:ApiKind = 'oneapi'
    }
    elseif ((Test-Variable 'ZIA_PASSWORD') -or (Test-Variable 'ZIA_API_KEY')) {
        $script:ApiKind = 'legacy'
    }
    else {
        Read-Choice 'ZSCALER_API' "Zscaler API credentials ${Purpose}:" 'oneapi' @(
            'oneapi|OneAPI, for tenants on Zidentity', 'legacy|Legacy ZIA API')
        $script:ApiKind = $script:Answers['ZSCALER_API']
    }
    if ($script:ApiKind -eq 'oneapi') {
        Read-VariableAnswer 'ZSCALER_VANITY_DOMAIN' 'Zidentity vanity domain, for example acme for acme.zslogin.net' 'Get-DomainProblem'
        Read-VariableAnswer 'ZSCALER_CLOUD' 'Zscaler cloud, Enter for the production cloud' 'Get-DomainProblem' -Optional
    }
    else {
        Read-VariableAnswer 'ZIA_USERNAME' 'Admin user name for the legacy ZIA API' 'Get-UserNameProblem'
        Read-VariableAnswer 'ZIA_CLOUD' 'ZIA cloud name, for example zscalertwo' 'Get-DomainProblem'
    }
}

# Asks for the secrets of the Zscaler API that Request-Api found.
function Request-ApiSecrets {
    if ($script:ApiKind -eq 'oneapi') {
        Read-Secret 'ZSCALER_CLIENT_ID' 'OneAPI client ID'
        if (-not (Test-Variable 'ZSCALER_PRIVATE_KEY')) {
            Read-Secret 'ZSCALER_CLIENT_SECRET' 'OneAPI client secret'
        }
    }
    else {
        Read-Secret 'ZIA_PASSWORD' 'Admin password for the legacy ZIA API'
        Read-Secret 'ZIA_API_KEY' 'API key for the legacy ZIA API'
    }
}

# Asks for everything a deployment needs that was not given, or takes it from the answers
# of the last run, and sets ResourceGroup and Location. Secrets come last and are not saved.
function Request-Answers {
    if (Test-Path -LiteralPath $AnswersFile -PathType Leaf) {
        Import-Answers
    }
    if (-not $script:CanAsk) {
        Write-Host "-ResourceGroup is not set. The values are taken from the answers in $AnswersFile."
    }
    else {
        Write-Host '-ResourceGroup is not set, so the values of the deployment are asked for here.'
        if ($script:Saved.Count -gt 0) {
            Write-Host ''
            Write-Host "Answers of the last run, from ${AnswersFile}:"
            foreach ($name in $answerNames) {
                if ($script:Saved.ContainsKey($name)) {
                    Write-Host "  $name = $($script:Saved[$name])"
                }
            }
            if (Read-YesNo 'Use these answers again?' $true) {
                $script:AcceptSaved = $true
            }
        }
        if (-not $script:AcceptSaved) {
            Write-Host 'Enter keeps the value in [brackets]. Ctrl+C stops. Nothing is deployed before you confirm at the end.'
            Write-Host ''
        }
    }

    Read-Answer 'RESOURCE_GROUP' 'Resource group, used as it is when it exists and created when it does not' 'Get-GroupProblem' (Get-FileValue 'newResourceGroupName')
    $script:ResourceGroup = $script:Answers['RESOURCE_GROUP']
    Read-Group
    if ($script:GroupMode -eq 'existing') {
        if (-not $script:AcceptSaved) {
            Write-Host "  $ResourceGroup exists in $script:GroupLocation and is used as it is."
        }
    }
    else {
        if (-not $script:AcceptSaved) {
            Write-Host "  $ResourceGroup does not exist and will be created."
        }
        if (-not $script:Location) {
            $script:Regions = @(Get-AzJson -Arguments @('account', 'list-locations') |
                    Where-Object { (Get-Property (Get-Property $_ 'metadata') 'regionType') -eq 'Physical' } |
                    ForEach-Object { $_.name })
            Read-Answer 'LOCATION' 'Azure region for the resource group and everything in it, for example eastus' 'Get-LocationProblem'
            $script:Location = $script:Answers['LOCATION']
        }
    }

    Read-Answer 'vmName' 'Name of the NSS VM' 'Get-VmNameProblem' (Get-FileValue 'vmName')
    Read-Choice 'virtualMachineSize' 'Size of the VM:' (Get-FileValue 'virtualMachineSize') $vmSizeOptions
    Read-Choice 'vhdRegion' 'Zscaler storage region the NSS image is copied from. It is the one your SAS token is for:' (Get-FileValue 'vhdRegion') $vhdRegionOptions
    Read-SshSource

    Read-Choice 'vNetNewOrExisting' 'Virtual network:' (Get-FileValue 'vNetNewOrExisting') @(
        'new|Create a new virtual network', 'existing|Use an existing virtual network')
    if ((Get-Current 'vNetNewOrExisting') -ceq 'existing') {
        Read-Answer 'virtualNetworkName' 'Name of the existing virtual network' 'Get-NetworkNameProblem'
        Read-Answer 'virtualNetworkResourceGroup' "Resource group of that virtual network, Enter when it is $ResourceGroup" 'Get-GroupProblem' -Optional
        Read-Answer 'subnetName' 'Name of its subnet for the service interface' 'Get-SubnetNameProblem'
        Read-Answer 'subnetName2' 'Name of its subnet for the management interface' 'Get-SubnetNameProblem'
    }
    else {
        Read-YesNoAnswer 'NETWORK_DEFAULTS' "Use the names and address ranges of the parameter file for it ($(Get-FileValue 'virtualNetworkName'), $(Get-FileValue 'vNetAddressRange'))?" 'true'
        if ((Get-Current 'NETWORK_DEFAULTS') -ceq 'false') {
            Read-Answer 'virtualNetworkName' 'Name of the virtual network' 'Get-NetworkNameProblem' (Get-FileValue 'virtualNetworkName')
            Read-Answer 'subnetName' 'Name of the service subnet' 'Get-SubnetNameProblem' (Get-FileValue 'subnetName')
            Read-Answer 'subnetName2' 'Name of the management subnet' 'Get-SubnetNameProblem' (Get-FileValue 'subnetName2')
            Read-Ranges
        }
    }

    Read-Choice 'stgNewOrExisting' 'Storage account the NSS image is copied to:' (Get-FileValue 'stgNewOrExisting') @(
        'new|Create a new storage account', "existing|Use an existing storage account of $ResourceGroup")
    if ((Get-Current 'stgNewOrExisting') -ceq 'existing') {
        Read-Answer 'nameStorageAccount' 'Name of the existing storage account' 'Get-StorageNameProblem'
    }

    Read-YesNoAnswer 'installNssCertificate' 'Register the VM in ZIA, install its certificate and start NSS at the end of the deployment?' (Get-FileValue 'installNssCertificate')
    $certificate = (Get-Current 'installNssCertificate') -ceq 'true'
    if ($certificate) {
        Read-Answer 'nssServerName' 'Name of the NSS server object in ZIA, reused when it exists and created when it does not' 'Get-ServerNameProblem' (Get-Current 'vmName')
        Read-Choice 'nssServerType' 'Type of the NSS server object, used when it is created:' (Get-FileValue 'nssServerType') $serverTypeOptions
        if ((Get-Current 'vNetNewOrExisting') -ceq 'existing') {
            if (-not $script:AcceptSaved) {
                Write-Host 'The certificate step runs in a subnet of the virtual network. The subnet must exist, be delegated to'
                Write-Host 'Microsoft.ContainerInstance/containerGroups, have the Microsoft.Storage service endpoint and reach the internet.'
            }
            Read-Answer 'certificateSubnetName' 'Name of that subnet' 'Get-SubnetNameProblem'
        }
        elseif ((Get-Current 'NETWORK_DEFAULTS') -ceq 'false') {
            Read-Answer 'certificateSubnetName' 'Name of the subnet the certificate step runs in' 'Get-SubnetNameProblem' (Get-FileValue 'certificateSubnetName')
            Read-CertificateRange
        }

        Request-Api 'for the certificate step'
    }

    if ($script:Asked) {
        Write-Host ''
        Save-Answers
    }

    if (Select-String -LiteralPath $ParameterFile -SimpleMatch "readEnvironmentVariable('NSS_SAS_TOKEN')" -Quiet) {
        Read-Secret 'NSS_SAS_TOKEN' 'SAS token of the NSS image, from Zscaler Support' 'Get-SasTokenProblem'
    }
    if ($certificate) {
        Read-Secret 'NSS_VM_PASSWORD' 'Password of the zsroot account on the NSS VM'
        Request-ApiSecrets
    }
    Write-Host ''
}

# True when the value the deployment will use for a boolean parameter is true: the last
# value given in -Parameters, otherwise the answer, otherwise the value in the parameter file.
function Test-ParameterIsTrue {
    param([string] $Name)
    $value = ''
    foreach ($pair in $Parameters) {
        if ($pair.StartsWith("$Name=")) {
            $value = $pair.Substring($Name.Length + 1)
        }
    }
    if (-not $value -and $script:Answers.ContainsKey($Name)) {
        $value = $script:Answers[$Name]
    }
    if (-not $value) {
        if (Select-String -LiteralPath $ParameterFile -Pattern "^param\s+$Name\s*=\s*true" -Quiet) {
            $value = 'true'
        }
    }
    return $value -eq 'true'
}

# Decides between a new and an existing resource group and returns the arguments that
# 'az deployment sub what-if' and 'az deployment sub create' share.
function Get-DeployArguments {
    if (-not $ResourceGroup -and -not $script:Questions) {
        Stop-Script "Set -ResourceGroup, for example: .\scripts\deploy.ps1 $Action -ResourceGroup <resource-group-name> -Location <location>. In a console, '.\scripts\deploy.ps1 $Action' without it asks for the values."
    }
    if (-not (Test-Path -LiteralPath $ParameterFile -PathType Leaf)) {
        Stop-Script "The parameter file '$ParameterFile' does not exist."
    }
    if (-not (Select-String -LiteralPath $ParameterFile -Pattern 'main\.sub\.bicep' -Quiet)) {
        Stop-Script "'$ParameterFile' is not a parameter file for main.sub.bicep. Its first line must be: using './main.sub.bicep'"
    }
    if ($script:Questions) {
        Assert-AzureCli
        # Ctrl+C ends the script inside the questions; the finally block says that nothing happened.
        $script:Asking = $true
        try {
            Request-Answers
            $script:Asking = $false
        }
        finally {
            if ($script:Asking) {
                Write-Host ''
                Write-Host "Stopped. $script:NothingDone"
            }
        }
    }
    $readsToken = Select-String -LiteralPath $ParameterFile -SimpleMatch "readEnvironmentVariable('NSS_SAS_TOKEN')" -Quiet
    if ($readsToken -and -not (Test-Variable 'NSS_SAS_TOKEN')) {
        Stop-Script "NSS_SAS_TOKEN is not set. Set the SAS token from Zscaler Support first: `$env:NSS_SAS_TOKEN = '<sas-token>'"
    }
    foreach ($pair in $Parameters) {
        if ($pair -notmatch '^[^=]+=') {
            Stop-Script "-Parameters must hold name=value strings. This is not one: '$(($pair -split '=')[0])'"
        }
    }
    # The certificate step runs last and fails without the password, after everything else
    # was deployed. When the parameter file takes the password from a variable that is not
    # set, that is known now.
    $readsPassword = Select-String -LiteralPath $ParameterFile -SimpleMatch "readEnvironmentVariable('NSS_VM_PASSWORD'" -Quiet
    $passwordInline = @($Parameters | Where-Object { $_.StartsWith('nssVmPassword=') }).Count -gt 0
    if ((Test-ParameterIsTrue 'installNssCertificate') -and $readsPassword -and -not $passwordInline -and
        -not (Test-Variable 'NSS_VM_PASSWORD')) {
        Stop-Script "installNssCertificate is true and NSS_VM_PASSWORD is not set. Set the password of the zsroot account first: `$env:NSS_VM_PASSWORD = '<password>'"
    }

    Assert-AzureCli
    Set-DeploymentName

    Read-Group
    if ($script:GroupMode -eq 'existing') {
        if (-not $script:Location) {
            $script:Location = $script:GroupLocation
        }
        $groupArguments = @("existingResourceGroupName=$ResourceGroup", 'newResourceGroupName=')
    }
    else {
        if (-not $script:Location) {
            Stop-Script "The resource group '$ResourceGroup' does not exist. Set -Location to create it, for example: -Location eastus"
        }
        $groupArguments = @('existingResourceGroupName=', "newResourceGroupName=$ResourceGroup")
    }

    # The answers come before the values of -Parameters, so a value given there wins.
    $answered = @($answerNames | Where-Object { (Test-IsParameter $_) -and $script:Answers.ContainsKey($_) } |
            ForEach-Object { "$_=$($script:Answers[$_])" })

    return @(
        '--name', $script:DeploymentName, '--location', $script:Location,
        '--parameters', $ParameterFile, '--parameters'
    ) + $groupArguments + $answered + $Parameters
}

function Show-Summary {
    $set = @($secretVariables | Where-Object { Test-Variable $_ })
    $unset = @($secretVariables | Where-Object { -not (Test-Variable $_) })
    $replaced = @($Parameters | ForEach-Object { ($_ -split '=')[0] })
    $none = '(none)'

    Write-Host ''
    Write-Host "  Subscription:       $script:Subscription"
    if ($script:GroupMode -eq 'new') {
        Write-Host "  Resource group:     $ResourceGroup (does not exist, will be created)"
    }
    else {
        Write-Host "  Resource group:     $ResourceGroup (exists, will be used as it is)"
    }
    Write-Host "  Location:           $script:Location"
    Write-Host "  Parameter file:     $ParameterFile"
    Write-Host ('  Replaced values:    {0}' -f $(if ($replaced) { $replaced -join ' ' } else { $none }))
    if ($script:Questions) {
        $label = 'Answered values:'
        foreach ($name in $answerNames) {
            if ((Test-IsParameter $name) -and $script:Answers.ContainsKey($name)) {
                Write-Host ('  {0,-19} {1}={2}' -f $label, $name, $script:Answers[$name])
                $label = ''
            }
        }
    }
    Write-Host "  Deployment name:    $script:DeploymentName"
    Write-Host ('  Variables set:      {0}' -f $(if ($set) { $set -join ' ' } else { $none }))
    Write-Host ('  Variables not set:  {0}' -f $(if ($unset) { $unset -join ' ' } else { $none }))
    Write-Host ''

    if (Test-ParameterIsTrue 'installNssCertificate') {
        if (-not (Test-ZscalerCredentials)) {
            Write-Host '  WARNING: installNssCertificate is true and neither set of Zscaler API variables is complete.'
            Write-Host '           OneAPI: ZSCALER_CLIENT_ID, ZSCALER_VANITY_DOMAIN and ZSCALER_CLIENT_SECRET or ZSCALER_PRIVATE_KEY.'
            Write-Host '           Legacy: ZIA_USERNAME, ZIA_PASSWORD, ZIA_API_KEY and ZIA_CLOUD.'
            Write-Host '           The certificate step runs last. A missing value is only reported then, after the VM is deployed.'
            Write-Host ''
        }
    }
}

# Prints the new lines of the log of a deployment script. Progress lines of the VHD copy
# that follow each other are shown as one progress, the latest.
function Show-ScriptLog {
    param([string] $Name, [int] $Depth)
    $lines = @(Invoke-Az -Quiet -Arguments @(
            'deployment-scripts', 'show-log', '--resource-group', $script:TargetGroup, '--name', $Name,
            '--query', 'value[0].log', '--output', 'tsv'))
    if ($LASTEXITCODE -ne 0 -or $lines.Count -eq 0 -or ($lines.Count -eq 1 -and $lines[0] -eq 'None')) {
        return
    }
    # Azure returns the log of the certificate script as one line. That script starts every line
    # it writes with "==> ", and the log is cut into lines again at these marks.
    $lines = @($lines | ForEach-Object { ("$_" -replace '^\s*==> ', '') -split '\s*==> ' })
    # The log ends with empty lines that must not be counted as lines that were shown.
    $count = $lines.Count
    while ($count -gt 0 -and -not "$($lines[$count - 1])".Trim()) {
        $count--
    }
    $shown = 0
    if ($script:LogLinesShown.ContainsKey($Name)) {
        $shown = $script:LogLinesShown[$Name]
    }
    $progress = $null
    for ($index = $shown; $index -lt $count; $index++) {
        $line = "$($lines[$index])".TrimEnd()
        # More than 16 digits are not expected and are shown as the line they came in.
        if ($line -match $progressPattern) {
            $copied = $Matches[2] -replace '[^0-9]', ''
            $total = $Matches[3] -replace '[^0-9]', ''
            if ($copied.Length -le 16 -and $total.Length -le 16 -and [long] $total -gt 0) {
                $progress = @([long] $copied, [long] $total)
                continue
            }
        }
        if ($progress) {
            Show-Progress -Name $Name -Depth $Depth -Copied $progress[0] -Total $progress[1]
            $progress = $null
        }
        if ($script:BarOwner -eq $Name) {
            Complete-Bar
        }
        Write-Step ('{0}  | {1}' -f ('  ' * $Depth), $line)
    }
    if ($progress) {
        Show-Progress -Name $Name -Depth $Depth -Copied $progress[0] -Total $progress[1]
    }
    if ($count -gt $shown) {
        $script:LogLinesShown[$Name] = $count
    }
}

# Reports every operation of a deployment whose state changed since the last check, and
# does the same for the deployments and deployment scripts it contains. A step that is
# still running is announced before its content, a step that ended after it.
function Show-Operations {
    param([string] $Scope, [string] $Deployment, [int] $Depth)
    if ($Scope -eq 'subscription') {
        $operations = Get-AzJson -Arguments @('deployment', 'operation', 'sub', 'list', '--name', $Deployment)
    }
    else {
        $operations = Get-AzJson -Arguments @(
            'deployment', 'operation', 'group', 'list', '--resource-group', $script:TargetGroup, '--name', $Deployment)
    }

    foreach ($operation in @($operations)) {
        $target = Get-Property (Get-Property $operation 'properties') 'targetResource'
        if (-not $target) {
            continue
        }
        $type = $target.resourceType
        $name = $target.resourceName
        $group = Get-Property $target 'resourceGroup'
        $state = $operation.properties.provisioningState
        $key = "$Depth|$type|$name"

        if ($type -eq 'Microsoft.Resources/resourceGroups') {
            $label = "resource group $name"
        }
        elseif ($type -eq 'Microsoft.Resources/deployments') {
            if ($Scope -eq 'subscription') {
                $label = "deployment into resource group $group"
            }
            else {
                $label = 'step ' + ($name -replace '-[a-z0-9]{13}$', '')
            }
        }
        else {
            $label = '{0} ({1})' -f $name, ($type -split '/')[-1]
        }

        if ($script:Finished.ContainsKey($key)) {
            continue
        }
        $changed = -not $script:States.ContainsKey($key) -or $script:States[$key] -ne $state
        $ended = $finishedStates -contains $state
        $announce = {
            if ($changed) {
                Write-Step ('{0}{1}: {2}' -f ('  ' * $Depth), $label, $state)
                $script:States[$key] = $state
                if ($state -eq 'Failed' -and $type -ne 'Microsoft.Resources/deployments') {
                    $script:Failed["$Scope|$Deployment"] = $true
                }
            }
        }

        if (-not $ended) {
            . $announce
        }
        if ($type -eq 'Microsoft.Resources/deployments') {
            if ($Scope -eq 'subscription' -and $group) {
                $script:TargetGroup = $group
            }
            if ($script:TargetGroup) {
                Show-Operations -Scope 'group' -Deployment $name -Depth ($Depth + 1)
            }
        }
        elseif ($type -eq 'Microsoft.Resources/deploymentScripts') {
            Show-ScriptLog -Name $name -Depth $Depth
            if ($ended -and $script:BarOwner -eq $name) {
                Complete-Bar
            }
        }
        if ($ended) {
            . $announce
            $script:Finished[$key] = $true
        }
    }
}

function Show-Failures {
    Write-Host ''
    if ($script:Failed.Count -gt 0) {
        Write-Host 'What failed:'
    }
    foreach ($entry in @($script:Failed.Keys | Sort-Object)) {
        $scope, $deployment = $entry -split '\|', 2
        if ($scope -eq 'subscription') {
            $operations = Get-AzJson -Arguments @('deployment', 'operation', 'sub', 'list', '--name', $deployment)
        }
        else {
            $operations = Get-AzJson -Arguments @(
                'deployment', 'operation', 'group', 'list', '--resource-group', $script:TargetGroup, '--name', $deployment)
        }
        foreach ($operation in @($operations)) {
            $properties = Get-Property $operation 'properties'
            $target = Get-Property $properties 'targetResource'
            if (-not $target -or $properties.provisioningState -ne 'Failed' -or
                $target.resourceType -eq 'Microsoft.Resources/deployments') {
                continue
            }
            $problem = Get-Property (Get-Property $properties 'statusMessage') 'error'
            Write-Host "  $($target.resourceName): $(Get-Property $problem 'code')"
            foreach ($line in ("$(Get-Property $problem 'message')" -split "`r?`n")) {
                if ($line.Trim()) {
                    Write-Host "    $line"
                }
            }
        }
    }
    Write-Host ''
    Write-Host 'Full error of the deployment:'
    Invoke-Az -Quiet -Arguments @(
        'deployment', 'sub', 'show', '--name', $script:DeploymentName, '--query', 'properties.error', '--output', 'yaml') |
        Out-Host
    Write-Host ''
    if ($script:LogLinesShown.Count -gt 0) {
        Write-Host 'The lines above marked with | are the log of a deployment script. Azure keeps it for one day.'
    }
    Write-Host 'After correcting the cause, run the same command again. Resources that were deployed are kept and reused.'
}

function Show-Outputs {
    $deployment = Get-AzJson -Arguments @('deployment', 'sub', 'show', '--name', $script:DeploymentName)
    if (-not $deployment) {
        Stop-Script "The deployment '$script:DeploymentName' cannot be read in subscription $script:Subscription."
    }
    $outputs = Get-Property $deployment.properties 'outputs'
    if ($outputs -and @($outputs.PSObject.Properties).Count -gt 0) {
        foreach ($output in $outputs.PSObject.Properties) {
            Write-Host ('{0}: {1}' -f $output.Name, $output.Value.value)
        }
        $running = Get-Property $outputs 'nssRunning'
        if ($running -and $running.value -eq $false) {
            Write-Host ''
            Write-Host "nssRunning is false: NSS is not running on the virtual machine. See 'Start NSS' under"
            Write-Host "'After the deployment' in README.md."
        }
    }
    else {
        Write-Host "The deployment '$script:DeploymentName' has no outputs. Outputs exist after a deployment that succeeded."
    }
}

function Watch-Deployment {
    $state = ''
    $unreadable = 0
    $script:Watching = $true
    Initialize-Display
    Write-Host "Progress is checked every $PollSeconds seconds. Ctrl+C stops watching, not the deployment."
    try {
        while ($true) {
            $deployment = Get-AzJson -Arguments @('deployment', 'sub', 'show', '--name', $script:DeploymentName)
            if (-not $deployment) {
                $unreadable++
                if ($unreadable -ge 5) {
                    $script:Watching = $false
                    Stop-Script "The deployment '$script:DeploymentName' cannot be read in subscription $script:Subscription."
                }
            }
            else {
                $unreadable = 0
                $state = $deployment.properties.provisioningState
                Show-Operations -Scope 'subscription' -Deployment $script:DeploymentName -Depth 0
                if ($finishedStates -contains $state) {
                    break
                }
            }
            Wait-NextCheck
        }
        Complete-Bar
        $script:Watching = $false
    }
    finally {
        Complete-Bar
        if ($script:Watching) {
            Write-Host ''
            Write-Host "Stopped watching. The deployment '$script:DeploymentName' continues in Azure."
            Write-Host "Watch it again with: .\scripts\deploy.ps1 Watch -ResourceGroup $ResourceGroup"
        }
    }

    if ($state -eq 'Succeeded') {
        Write-Step 'The deployment succeeded.'
        Write-Host ''
        Show-Outputs
        Write-Host ''
        Write-Host "Next: see 'After the deployment' in README.md. A new NSS server object in ZIA is a pending"
        Write-Host 'change that this deployment does not activate.'
        return 0
    }
    Write-Step "The deployment ended with the state $state."
    Show-Failures
    return 1
}

# ---------------------------------------------------------------------------------------
# Destroy
# ---------------------------------------------------------------------------------------

# The release of zscaler-sdk-python the certificate step is tested with, as it is pinned in
# scripts/install-nss-certificate.sh.
$script:SdkRequirement = 'zscaler-sdk-python==1.11.5'

$script:RecordState = ''
$script:RecordNewGroup = ''
$script:ServerId = ''
$script:ServerName = ''
$script:ServerIdFrom = ''
$script:ZiaNote = ''
$script:GroupState = ''     # Succeeded, Deleting or gone
$script:GroupAction = ''    # delete, keep or gone
$script:GroupReason = ''
$script:Created = @()       # the ID of everything the deployment created
$script:Unread = @()        # the steps whose record cannot be read
$script:Ours = @()          # the resources in the resource group that the deployment created
$script:Others = @()        # the other resources in the resource group
$script:Inside = @()        # what the deployment created inside a resource that is not its own
$script:PythonCommand = ''

# Runs the Azure CLI and returns its output, what it wrote as errors, and its status.
function Invoke-AzWithErrors {
    param([Parameter(Mandatory)] [string[]] $Arguments)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $lines = @($null | & az @Arguments --only-show-errors 2>&1)
    }
    finally {
        $ErrorActionPreference = $previous
    }
    return @{
        Status = $LASTEXITCODE
        Output = @($lines | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] })
        Errors = @($lines | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] } | ForEach-Object { "$_" })
    }
}

# Reads the record of the deployment: its state, its resource group, and what it says
# about the NSS server object.
function Read-Record {
    $result = Invoke-AzWithErrors -Arguments @('deployment', 'sub', 'show', '--name', $script:DeploymentName, '--output', 'json')
    if ($result.Status -ne 0) {
        if (@($result.Errors | Where-Object { $_ -match 'DeploymentNotFound' }).Count -gt 0) {
            Stop-Script "There is no deployment '$script:DeploymentName' in subscription $script:Subscription. Destroy deletes what 'make deploy' or scripts/deploy.ps1 deployed, and finds it through the record of that deployment. Nothing was deleted."
        }
        $result.Errors | ForEach-Object { Write-Host $_ }
        Stop-Script "The deployment '$script:DeploymentName' cannot be read in subscription $script:Subscription. Nothing was deleted."
    }
    $properties = (($result.Output -join "`n") | ConvertFrom-Json).properties
    $script:RecordState = $properties.provisioningState
    if ($finishedStates -notcontains $script:RecordState) {
        Stop-Script "The deployment '$script:DeploymentName' has the state '$script:RecordState' and has not ended. Wait for it with '.\scripts\deploy.ps1 Watch', or stop it with 'az deployment sub cancel --name $script:DeploymentName', and run this again. Nothing was deleted."
    }

    $parameters = Get-Property $properties 'parameters'
    $group = Get-Property (Get-Property $parameters 'existingResourceGroupName') 'value'
    if (-not $group) {
        $group = Get-Property (Get-Property $parameters 'newResourceGroupName') 'value'
        $script:RecordNewGroup = $group
    }
    if (-not $group) {
        Stop-Script "The record of the deployment '$script:DeploymentName' names no resource group, so it is not a deployment of main.sub.bicep. Nothing was deleted."
    }
    if ($ResourceGroup -and $ResourceGroup -ne $group) {
        Stop-Script "The deployment '$script:DeploymentName' went into the resource group '$group', not into '$ResourceGroup'. Nothing was deleted."
    }
    $script:ResourceGroup = $group

    $script:ServerName = [string] (Get-Property (Get-Property $parameters 'nssServerName') 'value')
    $recorded = Get-Property (Get-Property (Get-Property $properties 'outputs') 'nssServerId') 'value'
    if ((Get-Property (Get-Property $parameters 'installNssCertificate') 'value') -ne $true) {
        $script:ZiaNote = 'none: the deployment did not register the VM in ZIA (installNssCertificate was not true)'
    }
    elseif ($recorded) {
        $script:ServerId = [string] $recorded
        $script:ServerIdFrom = 'record'
    }
}

# Adds the ID of everything a deployment created to Created, and does the same for the
# deployments it contains. Scope is subscription or group.
function Add-Created {
    param([string] $Scope, [string] $Deployment)
    if ($Scope -eq 'subscription') {
        $operations = @(Get-AzJson -Arguments @('deployment', 'operation', 'sub', 'list', '--name', $Deployment))
        if ($LASTEXITCODE -ne 0) {
            Stop-Script "The operations of the deployment '$Deployment' cannot be read. Nothing was deleted."
        }
    }
    else {
        $operations = @(Get-AzJson -Arguments @(
                'deployment', 'operation', 'group', 'list', '--resource-group', $ResourceGroup, '--name', $Deployment))
        if ($LASTEXITCODE -ne 0) {
            $script:Unread += $Deployment
            return
        }
    }
    foreach ($operation in $operations) {
        $properties = Get-Property $operation 'properties'
        # Only what the deployment created: an operation that reads belongs to a resource the
        # deployment uses and did not create, such as an existing storage account.
        if ((Get-Property $properties 'provisioningOperation') -ne 'Create') {
            continue
        }
        $target = Get-Property $properties 'targetResource'
        $id = Get-Property $target 'id'
        if (-not $id) {
            continue
        }
        switch (Get-Property $target 'resourceType') {
            'Microsoft.Resources/resourceGroups' { }
            'Microsoft.Resources/deployments' {
                # Only the deployments in the resource group of this deployment are followed.
                if ($id -like "*/resourceGroups/$ResourceGroup/providers/*") {
                    Add-Created -Scope 'group' -Deployment ($id -split '/')[-1]
                }
            }
            default { $script:Created += $id }
        }
    }
}

# Reads what is in the resource group and tells what the deployment created from the
# rest: Ours and Others. Returns false when the resource group cannot be listed.
function Read-Inventory {
    $present = @(Get-AzJson -Arguments @('resource', 'list', '--resource-group', $ResourceGroup) | Where-Object { $_ })
    if ($LASTEXITCODE -ne 0) {
        return $false
    }

    $created = @{}
    $ours = @{}
    foreach ($id in $script:Created) {
        $created[$id.ToLowerInvariant()] = $true
        # A resource of the resource group itself: not a part of another resource, and not
        # something attached to one, such as a role assignment.
        $parts = $id.ToLowerInvariant() -split '/'
        if ($parts.Count -eq 9 -and $parts[3] -eq 'resourcegroups' -and $parts[4] -eq $ResourceGroup.ToLowerInvariant() -and
            $parts[5] -eq 'providers') {
            $ours[$id.ToLowerInvariant()] = $true
        }
    }

    # A deployment script that failed leaves the storage account and the container it ran
    # in behind. Azure names them in the script, and they are deleted with it.
    $scripts = @(Get-AzJson -Arguments @('deployment-scripts', 'list', '--resource-group', $ResourceGroup) | Where-Object { $_ })
    if ($LASTEXITCODE -eq 0) {
        foreach ($item in $scripts) {
            if (-not $created.ContainsKey(([string] $item.id).ToLowerInvariant())) {
                continue
            }
            $status = Get-Property $item 'status'
            foreach ($name in @('storageAccountId', 'containerInstanceId')) {
                $supporting = Get-Property $status $name
                if ($supporting) {
                    $ours[([string] $supporting).ToLowerInvariant()] = $true
                }
            }
            # The ID in the record of the deployment is missing after a run that failed. The
            # certificate script of an earlier run that succeeded still has it.
            $server = Get-Property (Get-Property $item 'outputs') 'nssServerId'
            if (-not $script:ServerId -and -not $script:ZiaNote -and $server) {
                $script:ServerId = [string] $server
                $script:ServerIdFrom = 'script'
            }
        }
    }

    $script:Ours = @($present | Where-Object { $ours.ContainsKey(([string] $_.id).ToLowerInvariant()) })
    $script:Others = @($present | Where-Object { -not $ours.ContainsKey(([string] $_.id).ToLowerInvariant()) })

    # What the deployment created inside a resource that is not its own, such as the
    # container with the copied VHD in an existing storage account.
    $oursPresent = @{}
    foreach ($resource in $script:Ours) {
        $oursPresent[([string] $resource.id).ToLowerInvariant()] = $true
    }
    $script:Inside = @($script:Created | Where-Object {
            $parts = $_ -split '/'
            $parts.Count -gt 9 -and -not $oursPresent.ContainsKey(($parts[0..8] -join '/').ToLowerInvariant())
        } | Sort-Object -Unique)
    return $true
}

# Decides whether the resource group is deleted: GroupAction and GroupReason.
function Select-GroupAction {
    $others = $script:Others.Count
    if ($script:GroupState -eq 'gone') {
        $script:GroupAction = 'gone'
    }
    elseif ($script:GroupState -eq 'Deleting') {
        $script:GroupAction = 'delete'
        $script:GroupReason = 'it is already being deleted'
    }
    elseif ($others -gt 0) {
        $script:GroupAction = 'keep'
        $script:GroupReason = "it holds $others resources this deployment did not create"
        if ($others -eq 1) {
            $script:GroupReason = 'it holds 1 resource this deployment did not create'
        }
    }
    elseif ($KeepResourceGroup) {
        $script:GroupAction = 'keep'
        $script:GroupReason = 'KEEP_RESOURCE_GROUP or -KeepResourceGroup is set'
    }
    elseif ($DeleteResourceGroup) {
        $script:GroupAction = 'delete'
        $script:GroupReason = 'it holds nothing but this deployment, and KEEP_RESOURCE_GROUP=0 or -DeleteResourceGroup is set'
    }
    elseif ($script:RecordNewGroup) {
        $script:GroupAction = 'delete'
        $script:GroupReason = 'this deployment created it, and it holds nothing else'
    }
    elseif ($script:CanAsk) {
        Write-Host "The resource group '$ResourceGroup' holds nothing but this deployment, but it existed when the"
        Write-Host "deployment last ran, so the record does not show whether a deployment created it."
        $script:Asking = $true
        if (Read-YesNo 'Delete the resource group itself as well?' $false) {
            $script:GroupAction = 'delete'
            $script:GroupReason = 'it holds nothing but this deployment, and you chose to delete it'
        }
        else {
            $script:GroupAction = 'keep'
            $script:GroupReason = 'you chose to keep it'
        }
        $script:Asking = $false
        Write-Host ''
    }
    else {
        $script:GroupAction = 'keep'
        $script:GroupReason = 'it existed when the deployment last ran. KEEP_RESOURCE_GROUP=0 or -DeleteResourceGroup deletes it'
    }
}

# Decides what happens to the NSS server object, and gets what its deletion needs.
function Initialize-Zia {
    if ($NssServerId) {
        if ($NssServerId -cnotmatch '^[0-9]+$') {
            Stop-Script '-NssServerId or NSS_SERVER_ID must be the ID of the NSS server, which is a number. Nothing was deleted.'
        }
        $script:ServerId = $NssServerId
        $script:ServerIdFrom = 'setting'
        $script:ZiaNote = ''
    }
    if ($KeepNssServer) {
        if ($script:ServerId) {
            $script:ZiaNote = "the object with the ID $script:ServerId is kept, because KEEP_NSS_SERVER or -KeepNssServer is set"
        }
        elseif (-not $script:ZiaNote) {
            $script:ZiaNote = 'nothing is deleted, because KEEP_NSS_SERVER or -KeepNssServer is set'
        }
        $script:ServerId = ''
        return
    }
    if (-not $script:ServerId) {
        if (-not $script:ZiaNote) {
            $script:ZiaNote = 'nothing is deleted: the deployment recorded no NSS server ID, because its certificate step did not finish'
        }
        return
    }

    Find-Python
    if (Test-ZscalerCredentials) {
        return
    }
    if (-not $script:CanAsk) {
        Stop-Script "The NSS server object $script:ServerId has to be deleted in ZIA, and neither set of Zscaler API variables is complete. Set OneAPI (ZSCALER_CLIENT_ID, ZSCALER_VANITY_DOMAIN and ZSCALER_CLIENT_SECRET or ZSCALER_PRIVATE_KEY) or legacy (ZIA_USERNAME, ZIA_PASSWORD, ZIA_API_KEY and ZIA_CLOUD), or use -KeepNssServer to leave the object in ZIA. Nothing was deleted."
    }
    Write-Host "The NSS server object $script:ServerId is deleted in ZIA with the Zscaler API credentials."
    $script:Asking = $true
    if (Test-Path -LiteralPath $AnswersFile -PathType Leaf) {
        Import-Answers
        $script:AcceptSaved = $true
    }
    Request-Api 'to delete the NSS server object with'
    Request-ApiSecrets
    $script:Asking = $false
    Write-Host ''
    if (-not (Test-ZscalerCredentials)) {
        Stop-Script 'The Zscaler API variables that are set do not make one complete set. Nothing was deleted.'
    }
}

# Prints resources as type and name.
function Show-Resources {
    param($Resources)
    $Resources | ForEach-Object { '                      {0}  {1}' -f $_.type, $_.name } | Sort-Object | ForEach-Object { Write-Host $_ }
}

function Show-DestroySummary {
    Write-Host ''
    Write-Host "  Subscription:       $script:Subscription"
    Write-Host "  Deployment:         $script:DeploymentName ($script:RecordState)"
    $ours = $script:Ours.Count
    switch ($script:GroupAction) {
        'gone' {
            Write-Host "  Resource group:     $ResourceGroup no longer exists, so there is nothing to delete in Azure"
        }
        'delete' {
            Write-Host "  Resource group:     $ResourceGroup is DELETED with everything in it, because $script:GroupReason"
            if ($ours -gt 0) {
                Write-Host '  Deleted with it:'
                Show-Resources $script:Ours
            }
        }
        'keep' {
            Write-Host "  Resource group:     $ResourceGroup is kept, because $script:GroupReason"
            if ($ours -gt 0) {
                Write-Host "  DELETED in it:      the $ours resources this deployment created"
                Show-Resources $script:Ours
            }
            else {
                Write-Host '  DELETED in it:      nothing. No resource this deployment created is left in it'
            }
            if ($script:Others.Count -gt 0) {
                Write-Host '  Kept in it:         what this deployment did not create'
                Show-Resources $script:Others
            }
            if ($script:Inside.Count -gt 0) {
                Write-Host '  Also kept:          what the deployment created inside a resource that is not its own'
                $script:Inside | ForEach-Object { Write-Host "                      $_" }
            }
        }
    }
    if ($script:Unread.Count -gt 0) {
        Write-Host '  WARNING:            the record of these steps cannot be read, so what they created is not'
        Write-Host '                      known to be of this deployment and is kept:'
        $script:Unread | ForEach-Object { Write-Host ('                      {0}' -f ($_ -replace '-[a-z0-9]{13}$', '')) }
    }
    if ($script:ServerId) {
        if ($script:ServerIdFrom -eq 'setting') {
            Write-Host "  NSS server in ZIA:  the object with the ID $script:ServerId, from NSS_SERVER_ID, is DELETED"
        }
        else {
            Write-Host "  NSS server in ZIA:  '$script:ServerName' with the ID $script:ServerId is DELETED"
        }
        Write-Host '                      The deletion is a pending change in ZIA that is not activated here.'
    }
    else {
        Write-Host "  NSS server in ZIA:  $script:ZiaNote"
    }
    Write-Host "  Deployment record:  $script:DeploymentName is deleted at the end"
    Write-Host ''
}

function Confirm-Destroy {
    if ($ConfirmDestroy) {
        if ($ConfirmDestroy -cne $ResourceGroup) {
            Stop-Script "-ConfirmDestroy or CONFIRM_DESTROY is '$ConfirmDestroy', which is not the name of the resource group, '$ResourceGroup'. Nothing was deleted."
        }
        return
    }
    Write-Host 'This cannot be undone.'
    $script:Asking = $true
    $answer = Read-Host 'Type the name of the resource group to delete what is listed above, anything else to stop'
    $script:Asking = $false
    if ("$answer" -cne $ResourceGroup) {
        Write-Host 'Nothing was deleted.'
        exit 0
    }
}

function Test-PythonVersion {
    param([string] $Command)
    return (Invoke-PythonQuietly $Command @('-c', 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)')) -eq 0
}

# Finds the Python that runs scripts/delete-nss-server.py: PythonCommand.
function Find-Python {
    if ($Python) {
        if (-not (Test-PythonVersion $Python)) {
            Stop-Script "-Python or PYTHON is '$Python', which is not Python 3.10 or later. Nothing was deleted."
        }
        $script:PythonCommand = $Python
        return
    }
    foreach ($candidate in @('python', 'python3', 'py')) {
        if ((Get-Command $candidate -ErrorAction SilentlyContinue) -and (Test-PythonVersion $candidate)) {
            $script:PythonCommand = $candidate
            return
        }
    }
    Stop-Script 'The NSS server object is deleted with zscaler-sdk-python, which needs Python 3.10 or later, and none was found. Install it or give one with -Python, or use -KeepNssServer to leave the object in ZIA. Nothing was deleted.'
}

# Runs a Python command without input and returns its status. Its output is discarded.
function Invoke-PythonQuietly {
    param([string] $Command, [string[]] $Arguments)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $null | & $Command @Arguments *> $null
        return $LASTEXITCODE
    }
    catch {
        return 1
    }
    finally {
        $ErrorActionPreference = $previous
    }
}

# Deletes the NSS server object in ZIA with scripts/delete-nss-server.py.
function Remove-NssServer {
    $command = $script:PythonCommand
    if ((Invoke-PythonQuietly $command @('-c', 'import zscaler')) -ne 0) {
        Write-Step "Installing $script:SdkRequirement into a temporary Python environment, which is removed afterwards."
        $environment = Join-Path ([System.IO.Path]::GetTempPath()) ('nss-deploy-' + [System.IO.Path]::GetRandomFileName())
        $script:TemporaryDirectory = $environment
        if ((Invoke-PythonQuietly $command @('-m', 'venv', $environment)) -ne 0) {
            Stop-Script "'$command -m venv' failed, so zscaler-sdk-python could not be installed. Give a Python 3.10 or later that has it with -Python. Nothing was deleted."
        }
        $command = Join-Path $environment 'Scripts\python.exe'
        if (-not (Test-Path -LiteralPath $command -PathType Leaf)) {
            $command = Join-Path (Join-Path $environment 'bin') 'python'
        }
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $null | & $command -m pip install --quiet --disable-pip-version-check --no-cache-dir $script:SdkRequirement 2>&1 |
                ForEach-Object { Write-Host "$_" }
        }
        finally {
            $ErrorActionPreference = $previous
        }
        if ($LASTEXITCODE -ne 0) {
            Stop-Script "$script:SdkRequirement could not be installed. The message above is from pip. Nothing was deleted."
        }
    }

    # An ID that was given is taken as it is. The name the deployment recorded is only
    # compared for the ID the deployment recorded.
    $expected = ''
    if ($script:ServerIdFrom -ne 'setting') {
        $expected = $script:ServerName
    }
    Write-Step "Deleting the NSS server object $script:ServerId in ZIA."
    $script:StopNote = 'Stopped while the NSS server object was being deleted in ZIA. Nothing was deleted in Azure. Run this again to finish.'
    $before = @{ NSS_SERVER_ID = $env:NSS_SERVER_ID; NSS_SERVER_NAME = $env:NSS_SERVER_NAME }
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $env:NSS_SERVER_ID = $script:ServerId
        $env:NSS_SERVER_NAME = $expected
        $null | & $command (Join-Path $PSScriptRoot 'delete-nss-server.py') 2>&1 | ForEach-Object { Write-Host "  | $_" }
        $status = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previous
        $env:NSS_SERVER_ID = $before.NSS_SERVER_ID
        $env:NSS_SERVER_NAME = $before.NSS_SERVER_NAME
    }
    if ($status -ne 0) {
        Stop-Script 'The NSS server object was not deleted in ZIA, for the reason above, so nothing was deleted in Azure either. Correct the cause and run this again, give the ID with -NssServerId <id>, or use -KeepNssServer to leave the object in ZIA.'
    }
}

# Deletes the resource group and waits until it is gone.
function Remove-Group {
    $script:StopNote = "Stopped watching. The deletion of the resource group '$ResourceGroup' continues in Azure. Run this again to finish: it then waits for the deletion and deletes the record of the deployment."
    if ($script:GroupState -ne 'Deleting') {
        Invoke-Az -Arguments @('group', 'delete', '--name', $ResourceGroup, '--yes', '--no-wait') | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Stop-Script "The deletion of the resource group '$ResourceGroup' was not started. The message above is from Azure."
        }
        Write-Step "The deletion of the resource group '$ResourceGroup' was started. It takes several minutes."
    }
    else {
        Write-Step "The resource group '$ResourceGroup' is being deleted. It takes several minutes."
    }
    Write-Host "Progress is checked every $PollSeconds seconds. Ctrl+C stops watching, not the deletion."
    $last = ''
    $unexpected = 0
    $unreadable = 0
    while ($true) {
        $exists = "$(Invoke-Az -Quiet -Arguments @('group', 'exists', '--name', $ResourceGroup, '--output', 'tsv'))".Trim()
        if ($LASTEXITCODE -eq 0 -and $exists -eq 'false') {
            break
        }
        if ($LASTEXITCODE -ne 0 -or $exists -ne 'true') {
            $unreadable++
            if ($unreadable -ge 5) {
                Stop-Script "The resource group '$ResourceGroup' cannot be read in subscription $script:Subscription, so it is not known whether it is deleted. Run this again."
            }
        }
        else {
            $unreadable = 0
            $group = Get-AzJson -Arguments @('group', 'show', '--name', $ResourceGroup)
            $state = Get-Property (Get-Property $group 'properties') 'provisioningState'
            # Azure sets the state back when it could not delete the resource group.
            if ($state -and $state -ne 'Deleting') {
                $unexpected++
                if ($unexpected -ge 3) {
                    Stop-Script "Azure did not delete the resource group '$ResourceGroup': its state is '$state' again. The reason is in its activity log, for example a lock or a resource that cannot be deleted. Run this again after correcting it."
                }
            }
            else {
                $unexpected = 0
            }
            $resources = @(Get-AzJson -Arguments @('resource', 'list', '--resource-group', $ResourceGroup) | Where-Object { $_ })
            if ($LASTEXITCODE -eq 0 -and "$($resources.Count)" -ne $last) {
                $last = "$($resources.Count)"
                Write-Step "  resource group ${ResourceGroup}: $last resources left"
            }
        }
        Start-Sleep -Seconds $PollSeconds
    }
    Write-Step "The resource group '$ResourceGroup' is deleted."
}

# Deletes the resources the deployment created and keeps the resource group. The Azure
# CLI deletes what depends on something else first; what is left is tried again for as
# long as a try deletes something.
function Remove-Resources {
    $left = $script:Ours.Count
    if ($left -eq 0) {
        return
    }
    $script:StopNote = 'Stopped. A deletion that Azure has started continues. Run this again to delete what is left.'
    Write-Step "Deleting $left resources in the resource group '$ResourceGroup'. It takes several minutes, and nothing is printed while Azure deletes."
    while ($true) {
        # The VM and the deployment scripts first: most of the rest is in use by them.
        $first = @($script:Ours | Where-Object { $_.type -match '(?i)(virtualMachines|deploymentScripts|containerGroups)$' })
        $rest = @($script:Ours | Where-Object { $_.type -notmatch '(?i)(virtualMachines|deploymentScripts|containerGroups)$' })
        $ids = @(($first + $rest) | ForEach-Object { [string] $_.id })
        Invoke-Az -Arguments (@('resource', 'delete', '--ids') + $ids) | Out-Null
        if ($LASTEXITCODE -eq 0) {
            break
        }
        $before = $left
        if (-not (Read-Inventory)) {
            Stop-Script "The resources of the resource group '$ResourceGroup' cannot be listed, so it is not known what is left. Run this again."
        }
        $left = $script:Ours.Count
        if ($left -eq 0) {
            break
        }
        if ($left -ge $before) {
            Write-Host 'Not deleted:'
            Show-Resources $script:Ours
            Stop-Script 'Azure deleted none of the resources that are left, for the reason above. Correct it and run this again.'
        }
        Write-Step "  $left resources left. Trying those again."
    }
    Write-Step "The resources of the deployment in '$ResourceGroup' are deleted."
}

function Invoke-Destroy {
    $script:NothingDone = 'Nothing was deleted.'
    $script:TemporaryDirectory = ''
    try {
        $script:CanAsk = -not [Console]::IsInputRedirected
    }
    catch {
        $script:CanAsk = $false
    }

    $finished = $false
    try {
        Read-Record
        Write-Host "Destroy of the Zscaler NSS deployment '$script:DeploymentName'. Nothing is deleted before you confirm."

        $exists = "$(Invoke-Az -Arguments @('group', 'exists', '--name', $ResourceGroup, '--output', 'tsv'))".Trim()
        if ($LASTEXITCODE -ne 0 -or $exists -notin @('true', 'false')) {
            Stop-Script "Could not check whether the resource group '$ResourceGroup' exists. Nothing was deleted."
        }
        if ($exists -eq 'true') {
            $group = Get-AzJson -Arguments @('group', 'show', '--name', $ResourceGroup)
            $script:GroupState = Get-Property (Get-Property $group 'properties') 'provisioningState'
            if (-not $script:GroupState) {
                Stop-Script "Could not read the resource group '$ResourceGroup'. Nothing was deleted."
            }
            Add-Created -Scope 'subscription' -Deployment $script:DeploymentName
            if (-not (Read-Inventory)) {
                Stop-Script "The resources of the resource group '$ResourceGroup' cannot be listed. Nothing was deleted."
            }
        }
        else {
            $script:GroupState = 'gone'
        }
        Initialize-Zia
        Select-GroupAction
        Show-DestroySummary
        Confirm-Destroy

        if ($script:ServerId) {
            Remove-NssServer
        }
        switch ($script:GroupAction) {
            'delete' { Remove-Group }
            'keep' { Remove-Resources }
        }
        $script:StopNote = 'Stopped. Everything but the record of the deployment is deleted. Run this again to delete the record.'
        Invoke-Az -Arguments @('deployment', 'sub', 'delete', '--name', $script:DeploymentName) | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Stop-Script "The record of the deployment '$script:DeploymentName' was not deleted. The message above is from Azure. Everything else is deleted."
        }
        $script:StopNote = ''
        Write-Step "The record of the deployment '$script:DeploymentName' is deleted."
        Write-Host ''
        Write-Host 'Destroyed.'
        if ($script:ServerId) {
            Write-Host 'The deletion of the NSS server object is a pending change in ZIA until it is activated there.'
        }
        if ($script:GroupAction -eq 'keep') {
            Write-Host "The resource group '$ResourceGroup' is kept. 'az group delete --name $ResourceGroup' deletes it with everything in it."
        }
        $finished = $true
    }
    finally {
        if ($script:TemporaryDirectory -and (Test-Path -LiteralPath $script:TemporaryDirectory)) {
            Remove-Item -LiteralPath $script:TemporaryDirectory -Recurse -Force -ErrorAction SilentlyContinue
        }
        if (-not $finished) {
            if ($script:Asking) {
                Write-Host ''
                Write-Host "Stopped. $script:NothingDone"
            }
            elseif ($script:StopNote) {
                Write-Host ''
                Write-Host $script:StopNote
            }
        }
    }
}

$script:Location = $Location
$script:DeploymentName = $DeploymentName

# Without a resource group the values are asked for in a console. Where nothing can be asked,
# the answers of an earlier run are used when there are any.
if ($Action -in @('Preview', 'Deploy') -and -not $ResourceGroup) {
    $console = $false
    try {
        $console = -not [Console]::IsInputRedirected
    }
    catch {
        $console = $false
    }
    if ($console) {
        $script:Questions = $true
        $script:CanAsk = $true
    }
    elseif (Test-Path -LiteralPath $AnswersFile -PathType Leaf) {
        $script:Questions = $true
        $script:AcceptSaved = $true
    }
}

if ($Action -in @('Preview', 'Deploy', 'Watch', 'Outputs', 'Destroy', 'Help')) {
    Write-ZscalerLogo
}

switch ($Action) {
    'Preview' {
        $arguments = Get-DeployArguments
        Write-Host 'Preview of the deployment. Nothing is created or changed.'
        Show-Summary
        Invoke-Az -Arguments (@('deployment', 'sub', 'what-if') + $arguments) | Out-Host
        exit $LASTEXITCODE
    }
    'Deploy' {
        $arguments = Get-DeployArguments
        Write-Host 'Deployment of the Zscaler NSS VM.'
        Show-Summary
        if (-not $Yes) {
            $answer = Read-Host 'Deploy now? [y/N]'
            if ($answer -notin @('y', 'yes')) {
                Write-Host 'Nothing was deployed.'
                exit 0
            }
        }
        Invoke-Az -Arguments (@('deployment', 'sub', 'create') + $arguments + @('--no-wait')) | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Stop-Script 'The deployment was not started. The message above is from Azure.'
        }
        Write-Step "The deployment '$script:DeploymentName' was started."
        exit (Watch-Deployment)
    }
    'Watch' {
        Assert-AzureCli
        Set-DeploymentName
        exit (Watch-Deployment)
    }
    'Outputs' {
        Assert-AzureCli
        Set-DeploymentName
        Show-Outputs
        exit 0
    }
    'Destroy' {
        if ($KeepResourceGroup -and $DeleteResourceGroup) {
            Stop-Script 'Use -KeepResourceGroup or -DeleteResourceGroup, not both.'
        }
        Assert-AzureCli
        Set-DeploymentName
        Invoke-Destroy
        exit 0
    }
    default {
        Get-Help $PSCommandPath -Detailed
    }
}
