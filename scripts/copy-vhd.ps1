<#
.SYNOPSIS
    Copies the Zscaler NSS OS VHD into the deployment's storage account.

.DESCRIPTION
    Runs inside the Microsoft.Resources/deploymentScripts resource declared in
    modules/vhd-copy.bicep, which embeds this file at build time.

    Every input arrives as an environment variable so the SAS token and the
    storage account key are never written to the deployment history or to the
    script log.

    The copy is skipped when the destination blob already holds a completed
    copy, which keeps redeployments fast and independent of SAS token expiry.
#>

$ErrorActionPreference = 'Stop'

function Get-RequiredSetting {
    param([Parameter(Mandatory)][string] $Name)

    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "Environment variable '$Name' is not set."
    }
    return $value
}

$sourceVhdUrl = Get-RequiredSetting 'SOURCE_VHD_URL'
$sourceSasToken = Get-RequiredSetting 'SOURCE_SAS_TOKEN'
$storageAccountName = Get-RequiredSetting 'DEST_STORAGE_ACCOUNT'
$storageAccountKey = Get-RequiredSetting 'DEST_STORAGE_KEY'
$storageEndpointSuffix = Get-RequiredSetting 'DEST_STORAGE_ENDPOINT_SUFFIX'
$containerName = Get-RequiredSetting 'DEST_CONTAINER'
$blobName = Get-RequiredSetting 'DEST_BLOB'

$pollIntervalSeconds = 30

$context = New-AzStorageContext `
    -StorageAccountName $storageAccountName `
    -StorageAccountKey $storageAccountKey `
    -Endpoint $storageEndpointSuffix

$destination = @{
    Context   = $context
    Container = $containerName
    Blob      = $blobName
}

# Returns $null when the destination blob does not exist yet.
# The blob is looked up by listing rather than by name, because asking for a missing blob raises
# an error, and the deployment script service reports every recorded error, even a handled one.
function Get-ExistingCopyState {
    $existingBlob = Get-AzStorageBlob -Context $context -Container $containerName -Prefix $blobName |
        Where-Object { $_.Name -eq $blobName }

    if (-not $existingBlob) {
        return $null
    }
    return Get-AzStorageBlobCopyState @destination
}

$copyState = Get-ExistingCopyState

if ($copyState -and $copyState.Status -eq 'Success') {
    Write-Output "'$blobName' is already present in container '$containerName'. Skipping the copy."
}
else {
    if ($copyState -and $copyState.Status -eq 'Pending') {
        Write-Output "A copy of '$blobName' is already in progress. Waiting for it to finish."
    }
    else {
        Write-Output "Starting the copy of $sourceVhdUrl to '$containerName/$blobName'."
        try {
            Start-AzStorageBlobCopy `
                -AbsoluteUri "$sourceVhdUrl$sourceSasToken" `
                -DestContainer $containerName `
                -DestBlob $blobName `
                -DestContext $context `
                -Force | Out-Null
        }
        catch {
            $message = $_.Exception.Message.Replace($sourceSasToken, '?<redacted>')
            throw "Could not start the VHD copy. Check the VHD region and the SAS token. $message"
        }
    }

    do {
        Start-Sleep -Seconds $pollIntervalSeconds
        $copyState = Get-AzStorageBlobCopyState @destination
        Write-Output ('VHD copy {0}: {1:N0} of {2:N0} bytes copied.' -f $copyState.Status, $copyState.BytesCopied, $copyState.TotalBytes)
    } while ($copyState.Status -eq 'Pending')

    if ($copyState.Status -ne 'Success') {
        throw "The VHD copy ended with status '$($copyState.Status)': $($copyState.StatusDescription)"
    }

    Write-Output 'The VHD has been copied successfully.'
}
