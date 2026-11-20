metadata description = 'Network security group shared by the NSS network interfaces and the service subnet.'

@description('Azure region for the network security group.')
param location string

@description('Name of the NSS virtual machine. The network security group is named <vmName>-NSG.')
param vmName string

@description('Source IP address or CIDR range allowed to reach the NSS management interface over SSH.')
param sshSourceAddressPrefix string

// Outbound flows the NSS appliance needs, as listed in the Zscaler NSS deployment guide for Azure.
var outboundRules = [
  {
    name: 'z-smca-rule'
    description: 'Allow SMCA'
    protocol: 'Tcp'
    destinationPort: '9422'
    destinationAddressPrefix: '*'
    priority: 101
  }
  {
    name: 'z-remotesupport-rule'
    description: 'Allow Remote Support'
    protocol: 'Tcp'
    destinationPort: '12002'
    destinationAddressPrefix: '199.168.148.0/24'
    priority: 102
  }
  {
    name: 'z-nanolog-rule'
    description: 'Allow Nanolog'
    protocol: 'Tcp'
    destinationPort: '9431'
    destinationAddressPrefix: '*'
    priority: 103
  }
  {
    name: 'z-dns-rule'
    description: 'Allow DNS'
    protocol: '*'
    destinationPort: '53'
    destinationAddressPrefix: '*'
    priority: 104
  }
  {
    name: 'z-ntp-rule'
    description: 'Allow NTP'
    protocol: '*'
    destinationPort: '123'
    destinationAddressPrefix: '*'
    priority: 105
  }
  {
    name: 'z-cdss-rule'
    description: 'Allow SMCDSS'
    protocol: 'Tcp'
    destinationPort: '443'
    destinationAddressPrefix: '*'
    priority: 106
  }
]

resource networkSecurityGroup 'Microsoft.Network/networkSecurityGroups@2025-05-01' = {
  name: '${vmName}-NSG'
  location: location
  properties: {
    securityRules: concat(
      [
        {
          name: 'z-ssh-rule'
          properties: {
            description: 'Allow SSH from specified source address prefix'
            direction: 'Inbound'
            access: 'Allow'
            priority: 101
            protocol: 'Tcp'
            sourceAddressPrefix: sshSourceAddressPrefix
            sourcePortRange: '*'
            destinationAddressPrefix: '*'
            destinationPortRange: '22'
          }
        }
      ],
      map(outboundRules, rule => {
        name: rule.name
        properties: {
          description: rule.description
          direction: 'Outbound'
          access: 'Allow'
          priority: rule.priority
          protocol: rule.protocol
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: rule.destinationAddressPrefix
          destinationPortRange: rule.destinationPort
        }
      })
    )
  }
}

@description('Resource ID of the network security group.')
output id string = networkSecurityGroup.id

@description('Name of the network security group.')
output name string = networkSecurityGroup.name
