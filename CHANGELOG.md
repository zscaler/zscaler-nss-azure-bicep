# Changelog

## 0.1.0 (November, 20 2026)

### Notes

- Release date: **(November, 20 2026)**
- First release of [zscaler-nss-azure-bicep](https://github.com/zscaler/zscaler-nss-azure-bicep): a native Bicep deployment of the Zscaler NSS virtual machine. The README **Deploy to Azure**, **Deploy to Azure Government**, and **Visualize** buttons load `azuredeploy.json` from the `master` branch of this repository.

### Features

- Modular Bicep entry points: `main.bicep` at resource group scope and `main.sub.bicep` at subscription scope. The subscription deployment creates the resource group when it does not exist and deploys into it when it does.
- `azuredeploy.json` is the compiled resource group template used by the portal buttons and by customers who deploy with ARM directly.
- Optional certificate step (`installNssCertificate`): deployment script configures the service interface, registers or reuses the NSS server object in ZIA with OneAPI or legacy ZIA API credentials supplied at deploy time, downloads the certificate with `zscaler-sdk-python` 1.11.5, installs it on the VM, runs `update-now`, starts NSS, and enables automatic start. The step does not activate the ZIA configuration. The service network interface keeps a static private address. When NSS does not start, the deployment still succeeds and reports `nssRunning` as false.
- Command-line deployment for Linux, macOS, and Windows: `make preview`, `make deploy`, `make watch`, and `make outputs`, plus `scripts/deploy.ps1` with the same behaviour. Without a resource group, the scripts ask for deployment values interactively, offer the detected public IPv4 address as the default for the SSH rule, save answers in `.nss-deploy-answers`, and never write secrets to that file.
- `make destroy` and `scripts/deploy.ps1 Destroy` remove the NSS server object in ZIA by the ID recorded by the deployment, then delete Azure resources (the resource group when this deployment created it and it holds nothing else), then remove the subscription-level deployment record.
- `nssServerType` allows only the eleven documented NSS server types; the default is `NSS_FOR_WEB`.