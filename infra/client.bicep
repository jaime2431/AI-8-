// One deployment per client, into THAT client's own Azure subscription (bought through CSP).
// Creates: serverless Azure SQL database (the clean database), storage for documents and e-CF files,
// and a Key Vault for that client's secrets.
// Status: starter template, not yet deployed. Run `az deployment group what-if` before the first real deployment.
//
//   az group create -n rg-datia-<client> -l eastus2
//   az deployment group create -g rg-datia-<client> -f infra/client.bicep \
//      -p clientSlug=<client> sqlAdminGroupName='Datia SQL Admins' sqlAdminGroupObjectId=<group-object-id>

@description('Short client id, lowercase letters and numbers, max 9 characters, e.g. ferrenort')
@minLength(3)
@maxLength(9)
param clientSlug string

param location string = resourceGroup().location

@description('Entra group that administers the database (our BI team)')
param sqlAdminGroupName string
param sqlAdminGroupObjectId string

@description('Object id of the person or group running onboarding; gets permission to write this vault\'s secrets')
param deployerObjectId string
@allowed(['User', 'Group', 'ServicePrincipal'])
param deployerPrincipalType string = 'User'

var suffix = uniqueString(resourceGroup().id)
var tags = { client: clientSlug, managedBy: 'datia' }

resource sql 'Microsoft.Sql/servers@2021-11-01' = {
  name: 'sql-datia-${clientSlug}-${suffix}'
  location: location
  tags: tags
  properties: {
    minimalTlsVersion: '1.2'
    publicNetworkAccess: 'Enabled'
    administrators: {
      administratorType: 'ActiveDirectory'
      azureADOnlyAuthentication: true      // no SQL passwords; Entra identities only
      login: sqlAdminGroupName
      sid: sqlAdminGroupObjectId
      tenantId: subscription().tenantId
      principalType: 'Group'
    }
  }
}

resource cleanDb 'Microsoft.Sql/servers/databases@2021-11-01' = {
  parent: sql
  name: 'clean'
  location: location
  tags: tags
  sku: { name: 'GP_S_Gen5_1', tier: 'GeneralPurpose' }   // serverless: pauses when idle to keep cost low
  properties: {
    autoPauseDelay: 60
    minCapacity: json('0.5')
    zoneRedundant: false
  }
}

resource allowAzure 'Microsoft.Sql/servers/firewallRules@2021-11-01' = {
  parent: sql
  name: 'AllowAzureServices'
  properties: { startIpAddress: '0.0.0.0', endIpAddress: '0.0.0.0' }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: toLower('stdatia${clientSlug}${take(suffix, 6)}')
  location: location
  tags: tags
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
  }
}

resource blob 'Microsoft.Storage/storageAccounts/blobServices@2023-01-01' = {
  parent: storage
  name: 'default'
}

resource inbox 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  parent: blob
  name: 'inbox'
}

resource ecf 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  parent: blob
  name: 'ecf'
}

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: 'kv-datia-${clientSlug}-${take(suffix, 4)}'
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 30
    enablePurgeProtection: true
  }
}

// Identity the nightly job runs as for THIS client: it can read only this client's vault and storage.
resource jobIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-datia-${clientSlug}'
  location: location
  tags: tags
}

var kvSecretsUser = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
var blobContributor = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
var kvSecretsOfficer = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7')

resource deployerWritesVault 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: vault
  name: guid(vault.id, deployerObjectId, kvSecretsOfficer)
  properties: { roleDefinitionId: kvSecretsOfficer, principalId: deployerObjectId, principalType: deployerPrincipalType }
}

resource jobReadsVault 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: vault
  name: guid(vault.id, jobIdentity.id, kvSecretsUser)
  properties: { roleDefinitionId: kvSecretsUser, principalId: jobIdentity.properties.principalId, principalType: 'ServicePrincipal' }
}

resource jobUsesStorage 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, jobIdentity.id, blobContributor)
  properties: { roleDefinitionId: blobContributor, principalId: jobIdentity.properties.principalId, principalType: 'ServicePrincipal' }
}

output sqlServer string = sql.properties.fullyQualifiedDomainName
output sqlDatabase string = cleanDb.name
output jobIdentityName string = jobIdentity.name
output jobIdentityClientId string = jobIdentity.properties.clientId
output storageAccount string = storage.name
output keyVaultUri string = vault.properties.vaultUri
output keyVaultName string = vault.name
