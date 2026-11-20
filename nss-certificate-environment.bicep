metadata description = 'Private environment for the NSS certificate deployment script: its managed identity and the firewalled storage account it runs from.'

@description('Azure region for the identity and the storage account.')
param location string

@description('Name of the user-assigned managed identity the deployment script runs as.')
param identityName string

@description('Globally unique name of the storage account that backs the deployment script.')
@minLength(3)
@maxLength(24)
param storageAccountName string

@description('Resource ID of the subnet the deployment script runs in. It must have the Microsoft.Storage service endpoint.')
param subnetId string

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: identityName
  location: location
}

// Reachable only from the deployment script subnet and from trusted Azure services, which is
// what a deployment script needs to run inside a virtual network.
resource storageAccount 'Microsoft.Storage/storageAccounts@2025-06-01' = {
  name: storageAccountName
  location: location
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Deny'
      virtualNetworkRules: [
        {
          id: subnetId
          action: 'Allow'
        }
      ]
    }
  }
}

var storageFileDataPrivilegedContributorRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '69566ab7-960f-475b-8e7c-b3118f30c6bd')

resource roleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccount.id, identity.id, storageFileDataPrivilegedContributorRoleId)
  scope: storageAccount
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: storageFileDataPrivilegedContributorRoleId
  }
}

@description('Name of the user-assigned managed identity the deployment script runs as.')
output identityName string = identity.name

@description('Name of the storage account that backs the deployment script.')
output storageAccountName string = storageAccount.name
