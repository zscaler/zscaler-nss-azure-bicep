# Deploys the Zscaler NSS VM from Linux or macOS. The work is done by scripts/deploy.sh;
# on Windows use scripts/deploy.ps1. Run 'make' without a target for the list below.

.DEFAULT_GOAL := help
.PHONY: help preview deploy watch outputs destroy

# Values given on the command line (make deploy RESOURCE_GROUP=...) or already set in the
# environment are passed on to the script.
export RESOURCE_GROUP LOCATION PARAMETER_FILE PARAMETERS DEPLOYMENT_NAME POLL_SECONDS ASSUME_YES ANSWERS_FILE PUBLIC_IP_URL
export CONFIRM_DESTROY KEEP_NSS_SERVER KEEP_RESOURCE_GROUP NSS_SERVER_ID PYTHON

help:
	@bash scripts/deploy.sh logo
	@echo "Zscaler NSS on Azure"
	@echo
	@echo "  make deploy                                            Ask for the values, then deploy as below."
	@echo "  make preview                                           Ask for the values, then preview as below."
	@echo
	@echo "  make preview RESOURCE_GROUP=<name> LOCATION=<region>   Show what would be deployed. Changes nothing."
	@echo "  make deploy  RESOURCE_GROUP=<name> LOCATION=<region>   Deploy and report progress until it ends."
	@echo "  make watch   RESOURCE_GROUP=<name>                     Report the progress of a running deployment."
	@echo "  make outputs RESOURCE_GROUP=<name>                     Print the outputs of the last deployment."
	@echo "  make destroy RESOURCE_GROUP=<name>                     Delete what the deployment created, in Azure and in ZIA."
	@echo
	@echo "  RESOURCE_GROUP   Used as it is when it exists, created in LOCATION when it does not."
	@echo "                   Without it, preview and deploy ask for the values in the terminal, and"
	@echo "                   watch, outputs and destroy use the resource group that was answered."
	@echo "  LOCATION         Azure region. Optional for an existing resource group."
	@echo "  PARAMETER_FILE   Default: main.sub.bicepparam"
	@echo "  PARAMETERS       Values that replace those of the parameter file, for example"
	@echo "                   PARAMETERS=\"vhdRegion=znssprodeu installNssCertificate=true\""
	@echo "  ASSUME_YES=1     Deploy without the confirmation question."
	@echo "  ANSWERS_FILE     Where the answers are saved, without secrets. Default: .nss-deploy-answers"
	@echo "  PUBLIC_IP_URL    Service asked for your public IP address, which is offered for the SSH rule."
	@echo "                   Default: https://api.ipify.org, then https://checkip.amazonaws.com. none: no service."
	@echo
	@echo "make destroy lists what it deletes and asks for the name of the resource group before it deletes"
	@echo "anything. It deletes the NSS server object in ZIA, then the resource group when the deployment"
	@echo "created it and it holds nothing else, otherwise only the resources the deployment created."
	@echo "  CONFIRM_DESTROY=<name>   The name of the resource group: destroy without the question."
	@echo "  KEEP_NSS_SERVER=1        Leave the NSS server object in ZIA."
	@echo "  KEEP_RESOURCE_GROUP=1    Keep the resource group and delete only what the deployment created."
	@echo "                           0 deletes a resource group that existed before, when it holds nothing else."
	@echo "  NSS_SERVER_ID=<id>       Delete this NSS server object instead of the one the deployment recorded."
	@echo "  PYTHON                   Python 3.10 or later that deletes the NSS server object. Default: python3"
	@echo "The Zscaler API variables of the deployment (ZSCALER_* or ZIA_*) are needed to delete the object."
	@echo
	@echo "With RESOURCE_GROUP, export NSS_SAS_TOKEN first. Without it, a secret that is not exported is"
	@echo "asked for and not shown. The steps are described in README.md, 'Deployment Instructions'."

preview:
	@bash scripts/deploy.sh preview

deploy:
	@bash scripts/deploy.sh deploy

watch:
	@bash scripts/deploy.sh watch

outputs:
	@bash scripts/deploy.sh outputs

destroy:
	@bash scripts/deploy.sh destroy
