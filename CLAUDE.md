# CLAUDE.md

Guidance for AI agents working in this repository.

## What this repository is

A Bicep deployment that launches a single Zscaler Nanolog Streaming Service (NSS) virtual machine in Azure, in commercial or US Government clouds. It is published for Zscaler customers, who deploy it from the Azure Portal (**Deploy to Azure** button), the Azure CLI or Azure PowerShell. `README.md` is the customer-facing documentation.

A deployment does three things, in this order:

1. Creates the network security group and, optionally, a virtual network with a NAT gateway and a storage account.
2. Runs a deployment script that copies the NSS OS VHD from a Zscaler storage account into a container of the customer's storage account. The copy needs a SAS token issued by Zscaler Support.
3. Imports the copied VHD as a managed disk and creates the VM with two network interfaces (management and service). The service interface keeps the private IP address Azure assigned to it as a static address.

When `installNssCertificate` is `true`, a fourth, optional step follows: a second deployment script configures the service interface of the VM (`sudo nss configure`), finds or creates the NSS server object in ZIA, downloads its SSL certificate, installs it on the VM, downloads the NSS binaries (`sudo nss update-now`), starts NSS (`sudo nss start`) and enables its automatic start (`sudo nss enable-autostart`), all over SSH. It runs inside the virtual network, because the VM has no VM agent and SSH is only open to `sshSourceAddressPrefix`. A deployment script can only run inside a virtual network with a user-assigned managed identity, a storage account that is firewalled to its subnet, a role assignment on that storage account and a subnet delegated to `Microsoft.ContainerInstance/containerGroups`.

## Layout

| Path | Purpose |
|------|---------|
| `main.bicep` | Entry point, resource group scope. Declares every public parameter and output and wires the modules together. `azuredeploy.json` and the portal button are built from it. |
| `main.sub.bicep` | Second entry point, subscription scope. Creates the resource group when `existingResourceGroupName` is empty, otherwise uses the existing one, and deploys `main.bicep` into it as a module. Declares the parameters and outputs of `main.bicep` again. |
| `modules/network-security-group.bicep` | NSG with the SSH inbound rule and the NSS outbound rules. |
| `modules/virtual-network.bicep` | New virtual network, two subnets, NAT gateway, its public IP and public IP prefix, and the certificate subnet when `installNssCertificate` is `true`. Deployed only when `vNetNewOrExisting` is `new`. |
| `modules/storage-account.bicep` | New storage account for the staged VHD. Deployed only when `stgNewOrExisting` is `new`. |
| `modules/vhd-copy.bicep` | Blob container and the deployment script that copies the VHD. |
| `modules/virtual-machine.bicep` | Management public IP, the management NIC, the managed OS disk and the VM. Deploys `modules/service-network-interface.bicep` twice. |
| `modules/service-network-interface.bicep` | The service NIC, with an address Azure picks or with a given static one. |
| `modules/nss-certificate-environment.bicep` | Managed identity, firewalled storage account and role assignment the certificate deployment script needs to run inside the virtual network. Deployed only when `installNssCertificate` is `true`. |
| `modules/nss-certificate.bicep` | The certificate deployment script (Azure CLI container), which also runs `nss configure`, `nss update-now`, `nss start` and `nss enable-autostart`. Deployed only when `installNssCertificate` is `true`, after the VM. |
| `Makefile` | `preview`, `deploy`, `watch`, `outputs` and `destroy` targets for Linux and macOS. Each one calls `scripts/deploy.sh` and passes the variables on. |
| `scripts/deploy.sh` | One-command deployment for Linux and macOS (bash 3.2 and later): checks the input, decides between a new and an existing resource group with `az group exists`, starts `main.sub.bicep` with `az deployment sub create --no-wait` and reports the deployment operations and the deployment script logs until the deployment ends. Without `RESOURCE_GROUP`, `preview` and `deploy` first ask for the values in the terminal and save the answers. `destroy` deletes what a deployment created, see **Destroy** under Conventions. |
| `.nss-deploy-answers` | The saved answers of the person who ran a deploy script, as `name=value` lines without secrets. Ignored by Git, never committed. |
| `scripts/deploy.ps1` | The same for Windows (Windows PowerShell 5.1 and PowerShell 7). It also uses the Azure CLI, not Azure PowerShell. |
| `scripts/copy-vhd.ps1` | Script body of the VHD copy deployment script. `modules/vhd-copy.bicep` embeds it with `loadTextContent`. |
| `scripts/install-nss-certificate.py` | The certificate automation, with the NSS configuration before it and the automatic start after it: ZIA calls through `zscaler-sdk-python`, SSH through `paramiko`. Also documented in `README.md` as a script customers can run from a workstation. |
| `scripts/delete-nss-server.py` | Deletes one NSS server object in ZIA by its ID, with `delete_nss_server` of the SDK. Run on the workstation by the destroy action of the deploy scripts; it is not part of the template. It loads `scripts/install-nss-certificate.py` from its own directory for the connection to ZIA and the handling of errors. |
| `scripts/install-nss-certificate.sh` | Script body of the certificate deployment script. Creates a virtual environment, installs the pinned Python packages and runs the Python script. `modules/nss-certificate.bicep` inserts the Python source in place of the `__INSTALL_NSS_CERTIFICATE_PY__` line. |
| `tests/test_install_nss_certificate.py` | pytest tests for the Python script. |
| `tests/test_deploy_scripts.py`, `tests/fake_az.py` | pytest tests that run both deploy scripts and the Makefile against a fake Azure CLI, which answers from a scenario, evaluates the `--query` expressions with `jmespath` and records every command line. The tests of the progress bar and of the questions run the scripts in a pseudo-terminal. |
| `tests/test_delete_nss_server.py` | pytest tests for `scripts/delete-nss-server.py`, with a fake client and with the real SDK and intercepted HTTP calls. |
| `tests/test_destroy.py`, `tests/fake_zscaler/` | pytest tests for the destroy action of both deploy scripts and the Makefile, against the fake Azure CLI and a stand-in for `zscaler-sdk-python` that keeps the NSS servers of a fake tenant in a file. |
| `tests/test_bicep_entry_points.py` | pytest tests that compare the parameters and outputs of `main.sub.bicep` with those of `main.bicep`. They read the files as text and need no Bicep CLI. |
| `main.sub.bicepparam` | Parameters for `main.sub.bicep`: the resource group, a new virtual network and a new storage account. |
| `main.bicepparam` | Parameters for a new virtual network and a new storage account. |
| `main.existing.bicepparam` | Parameters for an existing virtual network and an existing storage account. |
| `bicepconfig.json` | Linter configuration. Enables rules that are off by default. |
| `azuredeploy.json` | ARM template compiled from `main.bicep`. Generated, never edited by hand. |
| `metadata.json` | Azure quickstart template metadata. |
| `.releaserc.json`, `.github/workflows/ci.yml` | semantic-release configuration and the release workflow that runs on `master`. |

## Build, lint and validate

The Bicep CLI is used through the Azure CLI (`az bicep ...`). The files are validated with Bicep CLI 0.49.2. If `az bicep version` reports that the CLI is missing, ask before installing anything.

Build and lint every Bicep file. Both commands must finish without errors or warnings:

```bash
for f in main.bicep main.sub.bicep modules/*.bicep; do
  az bicep build --file "$f" --stdout > /dev/null
  az bicep lint --file "$f"
done
```

The parameter files read the SAS token from the `NSS_SAS_TOKEN` environment variable and do not compile without it. They also read the VM password and the Zscaler API credentials (`NSS_VM_PASSWORD`, `ZSCALER_*`, `ZIA_*`), with an empty default. Use a placeholder value to validate them, and never print the compiled output, because it contains whatever those variables hold:

```bash
export NSS_SAS_TOKEN=placeholder
for f in *.bicepparam; do
  az bicep build-params --file "$f" --stdout > /dev/null
  az bicep lint --file "$f"
done
unset NSS_SAS_TOKEN
```

Regenerate the compiled template after every change to `main.bicep`, a module or a file in `scripts/`, and commit it together with that change:

```bash
az bicep build --file main.bicep --outfile azuredeploy.json
```

Check the syntax of the copy script with the PowerShell parser when `pwsh` is available:

```bash
pwsh -NoProfile -Command '$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path scripts/copy-vhd.ps1), [ref]$null, [ref]$e); $e; exit $e.Count'
```

Check the deploy scripts after every change to one of them: `shellcheck` for the shell script when it is installed, the PowerShell parser for the other, and the tests.

```bash
shellcheck -s bash scripts/deploy.sh && /bin/bash -n scripts/deploy.sh
pwsh -NoProfile -Command '$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path scripts/deploy.ps1), [ref]$null, [ref]$e); $e; exit $e.Count'
```

What-if previews a deployment without changing resources. It needs an Azure login, a resource group and a real SAS token, so run it only when the user asks for it:

```bash
az deployment group what-if --resource-group <resource-group> --parameters main.bicepparam
az deployment sub what-if --location <location> --parameters main.sub.bicepparam
```

What-if and `validate` at subscription scope do not notice a resource group in `existingResourceGroupName` that does not exist. Only the deployment fails, with `ResourceGroupNotFound`.

Run the tests after every change to `scripts/install-nss-certificate.py`, `scripts/delete-nss-server.py`, a deploy script, the `Makefile`, `main.bicep` or `main.sub.bicep`. They need `pytest` and `paramiko`; the tests that go through the real SDK with intercepted HTTP calls also need `zscaler-sdk-python` 1.11.5 or later and `responses`, and the tests of the deploy scripts need `jmespath`, `bash` and, for `deploy.ps1`, `pwsh`. Tests whose requirements are missing are skipped, so check the number of skipped tests. Install these in a virtual environment outside the repository, and ask before installing anything globally:

```bash
python3 -m pytest tests
```

The tests replace ZIA with a fake client or with intercepted HTTP calls, and the NSS VM with a local SSH server that imitates the commands the script runs. They prove the logic and the SSH handling, not the behaviour of the NSS appliance or of the ZIA API. The tests of the deploy scripts replace the Azure CLI with a fake; they prove what the scripts ask the CLI to do and what they print for its answers, not how Azure answers. Apart from the comparison of the two entry points, there are no tests for the Bicep files, and there is no CI job that builds them or runs the tests.

## Conventions

- **Public interface.** The parameter names, the output names and the resource names (`<vmName>-NSG`, `<vmName>-MGMT-IP`, `<vmName>-MGMT-NIC`, `<vmName>-SRVC-NIC`, `<vmName>-SRVC-IP`, `<vmName>-PIP`, `<vmName>-NATGW`, `vhdstorage<unique>`) are inherited from the ARM template this deployment replaced. Customers have parameter files and deployed resources that depend on them. Names such as `subnetName2` or `stgNewOrExisting` are kept on purpose.
- **Parameters.** Every parameter has `@description`. Use `@allowed`, `@minLength`, `@maxLength` and `@secure` where they apply. The order of the parameters in `main.bicep` is the order of the fields in the portal form.
- **Two entry points.** `main.sub.bicep` exists so that a command-line deployment can create the resource group; a deployment at resource group scope cannot. It holds the resource group and the call of `main.bicep`, nothing else. Every parameter and output of `main.bicep` is declared in it again with the same decorators, type and default, in the same order, and passed on unchanged. Two parameters differ on purpose, because there is no resource group to take a default from: `location` defaults to `deployment().location`, and `virtualNetworkResourceGroup` is empty by default and replaced with the name of the target resource group. When you add, change or remove a parameter or output in `main.bicep`, make the same change in `main.sub.bicep` and, where it applies, in `main.sub.bicepparam`. `tests/test_bicep_entry_points.py` fails when the two differ.
- **Resource group.** `existingResourceGroupName` set means: use that resource group and do not create or change it. Empty means: create the resource group named in `newResourceGroupName`. Both empty is refused with `fail()`. Do not make the template guess whether a resource group exists; a template cannot test that.
- **Deploy scripts.** `scripts/deploy.sh` and `scripts/deploy.ps1` do the same thing and print the same lines; change them together and keep the Makefile targets, the actions and the options in step. Rules for both:
  - They only call the Azure CLI, and every call has `--only-show-errors`. The two exceptions are the lookup of the public IP address described below and the Python that `destroy` runs `scripts/delete-nss-server.py` with.
  - They never take a secret as an argument and never print one. They report the names of the variables that are set.
  - `deploy` asks for confirmation unless `ASSUME_YES=1` or `-Yes` is given. `deploy` and `destroy` are the only actions that change anything.
  - Progress reporting must never decide the result. A call that fails while reporting is ignored; the exit status comes from the state of the deployment alone: 0 for `Succeeded`, 1 otherwise.
  - `deploy.sh` must run in bash 3.2 (macOS): no associative arrays, no `mapfile`, no `${var,,}`. `deploy.ps1` must run in Windows PowerShell 5.1: no ternary operator, no `??`, no `&&`.
  - A deployment script log returned by the Azure CLI ends with empty lines. They are not counted as shown lines, otherwise new lines are skipped.
  - The progress of the VHD copy is read from the log lines `VHD copy <status>: <copied> of <total> bytes copied.` that `scripts/copy-vhd.ps1` writes. When that line changes, change the pattern in both deploy scripts; `tests/test_deploy_scripts.py` fails when the copy script no longer writes it. A line that does not match is printed as it is.
  - On a terminal that progress is one bar that is redrawn in place, and every other line is printed above it: remove the bar before printing and draw it again afterwards. Everywhere else (output to a file or a pipe, `TERM=dumb`, a terminal too narrow for a bar) it is one line for each new progress, with no carriage return and no escape sequence. `NO_COLOR` turns the colours off.
  - **Questions.** `preview` and `deploy` without a resource group ask for the values when the input is a terminal. With a resource group nothing is asked and the answers file is not read; pipelines and existing commands depend on that. Without a terminal the saved answers are used when the file exists, and a value that is missing ends the run instead of being asked for.
  - A question exists for the values most deployments set. Everything else stays in the parameter file. The default of a question is the saved answer, otherwise the value written in the parameter file. `sshSourceAddressPrefix` has no default on purpose: the file holds a placeholder.
  - To add a question, add its name to `ANSWER_NAMES` in `deploy.sh` and to `$answerNames` in `deploy.ps1` at the place where it is asked, and ask it in both scripts with the same text and the same check. A name with a lower-case letter is a parameter of `main.sub.bicep` and is passed to the deployment before the values of `PARAMETERS`, which therefore win; a name in capitals is a setting of the scripts or an environment variable. The menus hold the `@allowed` values of `main.bicep` in its order. The tests compare the two lists, the menus and the template.
  - The question about `sshSourceAddressPrefix` offers the public IP address of the computer, which the scripts get from the services in `PUBLIC_IP_URL` (`curl` in `deploy.sh`, `Invoke-RestMethod` in `deploy.ps1`; `none` turns it off). The request is made only when that question is really asked: not with a resource group, not without a terminal, not when `PARAMETERS` gives the value, and not when the saved answers are used again. Only an answer with status 200 that is nothing but one IPv4 address is taken, and it is only ever a default the person confirms; a saved answer stays the default. A service that fails or is slow must never fail the run. Do not add another request to a service outside Azure without an explicit request. The tests set `PUBLIC_IP_URL` to `none` or to a local server and never ask a service on the internet.
  - The answers file (`.nss-deploy-answers`, or `ANSWERS_FILE`) is read line by line as `name=value`. It is never sourced or dot-sourced, and only the names of the list are read. Both scripts read and write the same file, so `deploy.ps1` writes it without a byte order mark. No secret is ever written to it.
  - A secret that is not set is asked for without showing it and is set as an environment variable of the script only, which is how the Azure CLI gets it. `deploy.sh` turns the echo of the terminal off before it prints the question and restores it on every way out; `deploy.ps1` uses `Read-Host -AsSecureString`.
  - The checks of the answers spell out the letters and digits they allow (`deploy.sh`) or compare with case (`-cmatch` in `deploy.ps1`), because `[A-Z]` depends on the locale in bash and `\w` and `\d` match more than ASCII in .NET.
  - PowerShell asks the terminal where the cursor is before it reads a line. The pseudo-terminal of the tests answers that request; a test that types into `deploy.ps1` without reading its output first makes PowerShell take the typed text for the answer.
  - In `deploy.sh` a background loop redraws the bar. It must be ended before anything else is printed, on Ctrl+C and on exit, and it ends by itself when the script is gone. `deploy.ps1` redraws the bar only while it waits for the next check. The characters of the bar are built from their numbers there, because Windows PowerShell 5.1 reads a script without a byte order mark as ANSI; keep `deploy.ps1` plain ASCII.
- **Destroy.** `destroy` deletes what `deploy` created and nothing else. Rules for both scripts:
  - The record of the subscription-level deployment (`nss-<resource group>`) is the only source of what the deployment created: the targets of its operations with `provisioningOperation` `Create`, followed through the nested deployments in its resource group. An operation with `Read` belongs to a resource the deployment uses and did not create (`existing` in Bicep) and must never count as created. Do not find resources by name, by prefix or by tag. A nested deployment whose operations cannot be read counts as nothing: what it created is kept and the summary says so.
  - Only resources directly in the resource group are deleted (an ID of nine parts), together with the storage account and the container instance that a deployment script of the deployment names in its `status`. What the deployment created inside a resource that is not its own is listed as kept.
  - The resource group is deleted only when it holds nothing but those resources, and only when the record shows that the deployment created it (`newResourceGroupName` set and `existingResourceGroupName` empty) or the person asks for it. Another run of `deploy` into the group records it as existing, which is why the question exists. Do not delete a resource group that holds anything else.
  - The order is fixed: the NSS server object in ZIA, then Azure, then the deployment record. The record is what a second run needs, so it goes last. A failure in ZIA stops before Azure. Every step must be safe to run again after a run that was stopped.
  - The confirmation is the name of the resource group, typed or in `CONFIRM_DESTROY` / `-ConfirmDestroy`. `ASSUME_YES` and `-Yes` must not confirm a destroy. Nothing is deleted, installed or written before it.
  - A check that fails to read Azure ends the run. An answer that is neither `true` nor `false` from `az group exists` is not "the resource group is gone".
  - The NSS server object is deleted only by the ID the deployment recorded: its `nssServerId` output, or the output of its certificate deployment script after a later run that failed. It is never looked up by name, and the name ZIA has for that ID must be the `nssServerName` of the record. `NSS_SERVER_ID` / `-NssServerId` is the person's own choice and skips the comparison.
  - `scripts/delete-nss-server.py` gets the ID and the credentials as environment variables and follows the rules of the certificate script: `AutomationError` with a message that is safe to print, no traceback, no credential in the output, no activation and no closed session. It is run with the workstation's Python (3.10 or later, `PYTHON` / `-Python`); without the SDK there, the release in `SDK_REQUIREMENT` / `$script:SdkRequirement` is installed into a temporary virtual environment that is removed afterwards. That release is the pin of `scripts/install-nss-certificate.sh`; a test fails when the three differ.
  - The destroy action has been run against Azure only up to its confirmation, which reads and changes nothing. No deletion, in Azure or in ZIA, has been run outside the tests: the order in which `az resource delete --ids` deletes resources that depend on each other, and what ZIA answers when it refuses to delete an NSS server, are not observed.
- **Modules.** `main.bicep` holds no resources of its own except `existing` references. Each module derives its resource names from `vmName`, and the modules are deployed without explicit deployment names.
- **References.** Use symbolic references, `parent` and nested child resources. Do not build resource IDs with `resourceId()` and do not add `dependsOn` when an implicit dependency exists. Each of the two explicit `dependsOn` entries in `main.bicep`, and the one in `main.sub.bicep`, is explained by the comment above it.
- **Conditional modules.** Outputs of the conditional modules are read as `module!.outputs.x` inside a ternary on the same condition.
- **API versions.** The `use-recent-api-versions` linter rule is enabled. When it reports a version, pick one from the list it prints.
- **Secrets.** The SAS token and the storage account key reach the copy script only through `secureValue` environment variables. Parameter files read the SAS token with `readEnvironmentVariable('NSS_SAS_TOKEN')`. The VM password and the Zscaler API credentials follow the same pattern: `@secure` parameters with an empty default, read in the parameter files with `readEnvironmentVariable('<NAME>', '')` and passed to the certificate script as `secureValue` environment variables. Secure settings that are empty are filtered out and not passed at all. The default password of the NSS image is never written into this repository.
- **Cloud-specific endpoints.** Use `environment()` or resource properties such as `primaryEndpoints.blob`. The one exception is the Zscaler source URL in `modules/vhd-copy.bicep`, which has a linter suppression and a comment explaining it.
- **Linter suppressions.** Fix the cause of a warning. A `#disable-next-line` needs a comment that explains why the rule does not apply.
- **Copy script.** `scripts/copy-vhd.ps1` takes all input from environment variables, must be safe to run again and must fail with `throw` so the deployment fails with it. It must never print the SAS token or the storage account key.
- **Certificate script.** `scripts/install-nss-certificate.py` takes all input from environment variables and must be safe to run again: an NSS server object with the requested name is reused, a certificate that the marker file on the VM (`/home/zsroot/.nss-certificate-installed`) shows as installed is not installed again, and a service interface that already has the wanted values is not configured again. It fails by raising `AutomationError`, whose message must be safe to print, and exits with a non-zero status so the deployment fails with it; the one exception is NSS that does not start, see **Starting NSS**. It must never print the VM password, an API credential or a traceback. It contacts the VM before ZIA, so a wrong password, an unreachable VM or a VM on which the `nss` command is not found leaves nothing behind in the tenant.
- **The `nss` command.** On the NSS image, a command that sshd starts for an SSH session does not have the directory of `nss` in its `PATH`; `sudo nss ...` ends there with `sudo: nss: command not found`, although it works after an interactive login. The script therefore finds the command first (`NssVm.locate_nss`: the `PATH` of the session, then the `PATH` a login shell reports, then the directories in `NSS_DIRECTORIES`) and runs it by its full path with that `PATH`. Do not go back to a bare `sudo nss`. The fake VM of the tests behaves like the image in this respect.
- **NSS configuration.** `sudo nss configure` only takes its answers interactively. The script types them into the terminal session, in `NssVm.run(..., answers=...)`, and only for the four questions it knows (`NAME_SERVER_QUESTION`, `ADD_NAME_SERVER_QUESTION`, `SERVICE_ADDRESS_QUESTION`, `SERVICE_GATEWAY_QUESTION`), which are copied from a session on the NSS 5.2 image for Azure. It must never guess: a question it does not know and a question that is asked again after its answer both stop the run with the output of the VM. Name servers are kept (`n`). The answers come from the template: the address of `<vmName>-SRVC-NIC` with the prefix length of the service subnet, and `cidrHost(<service subnet>, 0)` as the gateway. The values are validated as plain IPv4 before they are typed. The script configures before it contacts ZIA and before the certificate, skips the configuration when its marker file (`/home/zsroot/.nss-service-interface-configured`) or `sudo nss dump-config` shows the same values, and compares with `dump-config` afterwards. The form in which `dump-config` shows the two values (`smnet_dev:<value>`) is assumed, not observed; when it is not found, the success line of `nss configure` decides.
- **Starting NSS.** When `NSS_START` is `true`, which the template always sets, the script follows the Zscaler guide after the certificate: `sudo nss update-now`, because the image comes without the NSS binaries and `sudo nss start` answers `NSS service not installed` before it, then `sudo nss start`, then `sudo nss status` in a new session after `START_SETTLE_SECONDS`. NSS runs when `status` prints `NSS service running with pid <n>` (`RUNNING_STATUS`, copied from the NSS 5.2 image for Azure). That line decides, not the exit statuses of `update-now` and `start`, which are not known for every case. NSS that already runs is neither updated nor started. `update-now` has its own time limit (`NSS_UPDATE_TIMEOUT_SECONDS`). NSS that does not start is the one failure that does not fail the run: the script logs `WARNING: NSS is not running` with the reason and reports `nssRunning` as `false`, which the template returns as an output and the deploy scripts point out. Do not turn this into an error without an explicit request. `sudo nss enable-autostart` runs afterwards when `NSS_ENABLE_AUTOSTART` is `true`, which the template also always sets. Whether `update-now` works while the NSS server object is still a pending change in ZIA has not been observed.
- **Service NIC address.** The address of the service interface is written into the NSS configuration, so `<vmName>-SRVC-NIC` has a static private address. A static address has to be named and the template does not know a free one, so `modules/virtual-machine.bicep` deploys `modules/service-network-interface.bicep` twice: first with an address Azure picks, then with that address as a static one. A later deployment turns the address dynamic for a moment in the first step; Azure keeps the address of an interface that is in use. Do not merge the two steps into one resource and do not add a parameter for the address.
- **Changes in ZIA.** The certificate script only ever creates the NSS server object. It does not change or delete an existing object. The only deletion is in `scripts/delete-nss-server.py`, which the destroy action runs after a confirmation and which deletes one object by its ID. Do not add a deletion to the certificate script or to the template.
- **No activation.** The certificate script never activates the ZIA configuration, and neither does `scripts/delete-nss-server.py`; the user does that in ZIA. Do not add a call to `client.zia.activate`, and do not close the Zscaler session (`with`, `__exit__`, `deauthenticate`): the SDK ends a ZIA session that made a change in order to activate the staged configuration. The session is left to expire. Do not add an activation parameter without an explicit request.
- **NSS server types.** `nssServerType` in `main.bicep` and `NSS_SERVER_TYPES` in the script allow the same eleven values. Keep the two lists and the list in `README.md` identical.
- **One certificate, one VM.** Zscaler requires that the certificate of an NSS server object is installed on only one active NSS VM. The script refuses an existing object that ZIA reports as `HEALTHY` unless `allowHealthyNssServer` is `true`. Do not weaken this check.
- **Script log.** The log of a deployment script is readable by anyone who can read the resource group. The certificate deployment script therefore sets `NSS_PRINT_DUMP_CONFIG` to `false`. Azure returns the log of this script (Azure CLI container) as one line, with a space in place of every line end. The Python script therefore starts every line it logs with `==> ` (`LOG_MARK`), and `deploy.sh` and `deploy.ps1` cut the log into lines again at that mark. A test fails when the mark and the two scripts no longer match. The error goes to the error output without the mark and as one line, because Azure reports it as the error of the deployment.
- **Python packages.** `scripts/install-nss-certificate.sh` installs `zscaler-sdk-python` and `paramiko` at pinned versions. When you raise a pin, run the tests against that version, and raise the SDK release in `scripts/deploy.sh` (`SDK_REQUIREMENT`) and `scripts/deploy.ps1` (`$script:SdkRequirement`) with it.
- **Certificate download.** The Python script downloads the certificate only with the SDK's `nss_download_certificate` and sends no request of its own; do not add one. The method needs SDK release 1.11.5 or later (`MINIMUM_SDK_VERSION`); release 1.11.4 defined it outside the `NssServersAPI` class, where it cannot be called. Without the method the script fails with a message that names the minimum version. The pin in `scripts/install-nss-certificate.sh` must not be lower than `MINIMUM_SDK_VERSION`.
- **Commits.** Conventional Commits. semantic-release reads the messages on `master`; per `.releaserc.json` a `feat` produces a patch release and a breaking change a minor release.

## Do not

- Do not run `scripts/install-nss-certificate.py` against a real NSS VM or a real Zscaler tenant, and do not call the Zscaler API in any other way, unless the user explicitly asks for it. It creates an NSS server object and changes the VM.
- Do not run `scripts/delete-nss-server.py` against a real Zscaler tenant, and do not run `make destroy`, `scripts/deploy.sh destroy` or `scripts/deploy.ps1 Destroy` with a confirmation (`CONFIRM_DESTROY`, `-ConfirmDestroy` or a typed answer), unless the user explicitly asks for it. They delete an NSS server object and Azure resources, and neither can be undone. Without a terminal and without a confirmation, destroy only reads and prints its summary.
- Do not open the SSH rule of the NSG to Azure address ranges or service tags to let a deployment script in. The certificate script runs inside the virtual network for that reason.
- Do not deploy to Azure or run any command that creates, changes or deletes Azure resources (`make deploy`, `make destroy`, `scripts/deploy.sh deploy`, `scripts/deploy.ps1 Deploy`, `az deployment ... create`, `az group create`, `az group delete`, `New-AzResourceGroupDeployment`, and so on) unless the user explicitly asks for it. `make preview`, `make watch` and `make outputs` only read.
- Do not commit a SAS token, a storage account key or any other secret. Do not commit files produced by `az bicep build-params`; they contain the SAS token in plain text.
- Do not make a deploy script save a secret, in the answers file or anywhere else, and do not commit `.nss-deploy-answers`.
- Do not edit `azuredeploy.json` by hand, and do not delete or rename it. The **Deploy to Azure** buttons in `README.md` load it from the `master` branch by URL.
- Do not rename or remove a parameter, an output or a resource name without calling it out as a breaking change and updating the migration notes in `README.md`.
- Do not reintroduce unmanaged disks (`osDisk.vhd`), Basic SKU public IPs or dynamic public IP allocation. Azure retired unmanaged disks on March 31, 2026 and Basic SKU public IPs on September 30, 2025.
- Do not replace a deployment script with an external script URL, and do not load the Python script through `supportingScriptUris`. Earlier releases downloaded their runbook from `master` at deployment time, which tied every released template to the current state of that branch.
- Do not change the NSG rules, the VM sizes, the VHD regions or the NSS image name (`nssImageName` in `modules/vhd-copy.bicep`) without a matching change in Zscaler's NSS documentation or an explicit request.
- Do not push to `master` and do not create releases or tags by hand. Releases are created by semantic-release.
