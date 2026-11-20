"""Tests for the destroy action of scripts/deploy.sh (with the Makefile) and scripts/deploy.ps1.

Both scripts are run against tests/fake_az.py, a stand-in for the Azure CLI, and delete the
NSS server object with scripts/delete-nss-server.py on top of tests/fake_zscaler, a stand-in
for zscaler-sdk-python. Nothing here contacts Azure or Zscaler. The tests prove what the
scripts ask the Azure CLI and the SDK to do, and in which order; they do not prove that
Azure deletes what it is asked to delete.

They need what test_deploy_scripts.py needs, and are skipped without it.
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from non_secrets import NOT_A_CLIENT_ID, NOT_A_TYPED_CLIENT_ID

from test_deploy_scripts import (
    BASH,
    CLOUD,
    BOOKKEEPING,
    CLIENT_SECRET,
    DEPLOYMENTS,
    GROUP,
    KINDS,
    MAKE,
    NESTED,
    OUTPUTS,
    PWSH,
    REPOSITORY,
    SCRIPTS,
    SECRETS,
    Result,
    Terminal,
    answers_file,
    command_for,
    lines_of,
    run_directory,
    save,
    scenario,
)

SUBSCRIPTION = "00000000-0000-0000-0000-000000000000"
DEPLOYMENT = f"nss-{GROUP}"
VM = "Microsoft.Compute/virtualMachines"
DISK = "Microsoft.Compute/disks"
NIC = "Microsoft.Network/networkInterfaces"
NSG = "Microsoft.Network/networkSecurityGroups"
VNET = "Microsoft.Network/virtualNetworks"
STORAGE = "Microsoft.Storage/storageAccounts"
IDENTITY = "Microsoft.ManagedIdentity/userAssignedIdentities"
CONTAINER_GROUP = "Microsoft.ContainerInstance/containerGroups"
SERVER_NAME = "Zscaler-NSS-VM"
SERVER_ID = 4242
SERVERS = [{"id": SERVER_ID, "name": SERVER_NAME, "state": "HEALTHY"}, {"id": 5, "name": "other", "state": "HEALTHY"}]
CONFIRM_QUESTION = "anything else to stop"
GROUP_QUESTION = "Delete the resource group itself as well?"


def rid(kind, name, group=GROUP):
    return f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{group}/providers/{kind}/{name}"


def resource(kind, name, group=GROUP):
    return {"id": rid(kind, name, group), "type": kind, "name": name}


def created(kind, name, how="Create", resource_id=None, group=GROUP):
    """An operation of a deployment, with what the scripts read of it for a destroy."""
    return {
        "operationId": name,
        "properties": {
            "provisioningOperation": how,
            "provisioningState": "Succeeded",
            "targetResource": {
                "resourceType": kind,
                "resourceName": name,
                "id": resource_id or rid(kind, name, group),
                "resourceGroup": group,
            },
        },
    }


def step(name, suffix="bbbbbbbbbbbbb"):
    return f"{name}-{suffix}"


COPY_SCRIPT = "Zscaler-NSS-VM-vhd-copy"
CERTIFICATE_SCRIPT = "Zscaler-NSS-VM-nss-certificate"
# What the deployment created in its resource group. The service interface is created by a
# deployment inside the deployment of the virtual machine, three levels down.
OURS = [
    resource(NSG, "Zscaler-NSS-VM-NSG"),
    resource(SCRIPTS, COPY_SCRIPT),
    resource(VM, "Zscaler-NSS-VM"),
    resource(DISK, "Zscaler-NSS-VM-osdisk"),
    resource(NIC, "Zscaler-NSS-VM-MGMT-NIC"),
    resource(NIC, "Zscaler-NSS-VM-SRVC-NIC"),
    resource(STORAGE, "nsscertabc"),
    resource(IDENTITY, "Zscaler-NSS-VM-certificate-identity"),
]
# What somebody else put into the resource group: a virtual network and a storage account
# the deployment uses and did not create.
THEIRS = [resource(VNET, "Customer-VNET"), resource(STORAGE, "customerstorage")]
VHD_CONTAINER = rid(STORAGE, "customerstorage") + "/blobServices/default/containers/zscalernssrprod"
ROLE_ASSIGNMENT = rid(STORAGE, "nsscertabc") + "/providers/Microsoft.Authorization/roleAssignments/1111"

GROUP_OPERATIONS = {
    NESTED: [
        created(DEPLOYMENTS, step("networkSecurityGroup")),
        created(DEPLOYMENTS, step("vhdCopy")),
        created(DEPLOYMENTS, step("virtualMachine")),
        created(DEPLOYMENTS, step("nssCertificateEnvironment")),
        BOOKKEEPING,
    ],
    step("networkSecurityGroup"): [created(NSG, "Zscaler-NSS-VM-NSG")],
    step("vhdCopy"): [
        created(SCRIPTS, COPY_SCRIPT),
        # The existing storage account is read for its key, and the container is created in it.
        created(STORAGE, "customerstorage", how="Read"),
        created(
            STORAGE + "/blobServices/containers", "customerstorage/default/zscalernssrprod", resource_id=VHD_CONTAINER
        ),
    ],
    step("virtualMachine"): [
        created(VM, "Zscaler-NSS-VM"),
        created(DISK, "Zscaler-NSS-VM-osdisk"),
        created(NIC, "Zscaler-NSS-VM-MGMT-NIC"),
        created(DEPLOYMENTS, step("serviceNic", "ccccccccccccc")),
        BOOKKEEPING,
    ],
    step("serviceNic", "ccccccccccccc"): [created(NIC, "Zscaler-NSS-VM-SRVC-NIC")],
    step("nssCertificateEnvironment"): [
        created(STORAGE, "nsscertabc"),
        created(IDENTITY, "Zscaler-NSS-VM-certificate-identity"),
        created(IDENTITY, "Zscaler-NSS-VM-certificate-identity", how="Read"),
        created("Microsoft.Authorization/roleAssignments", "1111", resource_id=ROLE_ASSIGNMENT),
    ],
}


def parameters(**values):
    base = {
        "existingResourceGroupName": "",
        "newResourceGroupName": GROUP,
        "installNssCertificate": True,
        "nssServerName": SERVER_NAME,
        "vmName": "Zscaler-NSS-VM",
        "sasToken": None,
    }
    base.update(values)
    # Azure returns a secure parameter without a value.
    return {name: {"type": "x"} if value is None else {"type": "x", "value": value} for name, value in base.items()}


def destroy_scenario(state="Succeeded", theirs=(), group_operations=None, **changes):
    """A deployment that ended, and the resource group it went into."""
    base = {
        "groups": {GROUP: "eastus"},
        "phases": [
            {
                "state": state,
                "subscription_operations": [
                    created(
                        "Microsoft.Resources/resourceGroups",
                        GROUP,
                        resource_id=f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{GROUP}",
                    ),
                    created(DEPLOYMENTS, NESTED),
                    BOOKKEEPING,
                ],
                "group_operations": GROUP_OPERATIONS if group_operations is None else group_operations,
            }
        ],
        "outputs": OUTPUTS,
        "parameters": parameters(),
        "resources": {GROUP: OURS + list(theirs)},
        "scripts": {GROUP: [{"id": rid(SCRIPTS, COPY_SCRIPT), "status": {}, "outputs": None}]},
    }
    base.update(changes)
    return scenario(**base)


EXISTED = {"parameters": parameters(existingResourceGroupName=GROUP, newResourceGroupName="")}


def python_for(tmp_path):
    """A Python for the scripts that finds the stand-in for zscaler-sdk-python first."""
    python = tmp_path / "python-with-the-fake-sdk"
    python.write_text(
        f'#!/bin/sh\nPYTHONPATH="{REPOSITORY / "tests" / "fake_zscaler"}" exec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    python.chmod(0o755)
    return python


def destroy_environment(tmp_path, confirm=GROUP, servers=SERVERS, environment=None):
    zia = tmp_path / "zia"
    zia.mkdir(exist_ok=True)
    if not (zia / "servers.json").exists():
        (zia / "servers.json").write_text(json.dumps(servers), encoding="utf-8")
    variables = {"FAKE_ZIA_DIR": str(zia), "PYTHON": str(python_for(tmp_path))}
    if confirm is not None:
        variables["CONFIRM_DESTROY"] = confirm
    variables.update(environment or {})
    return variables


def read_fakes(result, tmp_path, directory):
    """Adds what the two fakes were asked to do, and in which order."""
    zia = tmp_path / "zia"
    result.zia = lines_of(zia / "calls.jsonl")
    result.zia_calls = [call["call"] for call in result.zia]
    result.servers = json.loads((zia / "servers.json").read_text(encoding="utf-8"))
    order = directory / "order"
    result.order = order.read_text(encoding="utf-8").splitlines() if order.exists() else []
    result.group_deletions = result.calls_of("group", "delete")
    result.resource_deletions = result.calls_of("resource", "delete")
    result.record_deletions = result.calls_of("deployment", "sub", "delete")
    return result


def destroy(
    kind, tmp_path, the_scenario=None, *, confirm=GROUP, servers=SERVERS, environment=None, arguments=(), **settings
):
    """Runs a destroy where nothing can be asked."""
    settings.setdefault("location", "")
    command, env, directory = command_for(
        kind,
        "destroy",
        tmp_path,
        the_scenario or destroy_scenario(),
        environment=destroy_environment(tmp_path, confirm, servers, environment),
        **settings,
    )
    completed = subprocess.run(
        command + list(arguments), env=env, input="", capture_output=True, text=True, timeout=180
    )
    return read_fakes(Result(completed, directory), tmp_path, directory)


def destroy_in_terminal(
    kind, tmp_path, the_scenario=None, answers=(), *, servers=SERVERS, environment=None, **settings
):
    """Runs a destroy in a terminal and types the answers to its questions."""
    settings.setdefault("location", "")
    directory = run_directory(tmp_path)
    terminal = Terminal(
        kind,
        "destroy",
        directory,
        the_scenario or destroy_scenario(),
        environment=destroy_environment(tmp_path, None, servers, environment),
        **settings,
    )
    for question, text in answers:
        terminal.answer(question, text)
    terminal.finish()
    return read_fakes(terminal, tmp_path, terminal.directory)


def nothing_was_deleted(result):
    return (
        result.group_deletions == []
        and result.resource_deletions == []
        and result.record_deletions == []
        and "delete" not in result.zia_calls
    )


def deleted_ids(call):
    ids = []
    for argument in call[call.index("--ids") + 1 :]:
        if argument.startswith("-"):
            break
        ids.append(argument)
    return ids


def sha256(text):
    return hashlib.sha256(text.encode()).hexdigest()


# --------------------------------------------------------------------------------------
# The resource group the deployment created
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_destroy_deletes_the_server_then_the_group_then_the_record(kind, tmp_path):
    result = destroy(kind, tmp_path)

    assert result.status == 0, result.output
    assert result.zia_calls == ["connect", "list", "delete"]
    assert result.zia[-1]["id"] == SERVER_ID
    assert result.servers == SERVERS[1:]
    (call,) = result.group_deletions
    assert call[call.index("--name") + 1] == GROUP and "--yes" in call and "--no-wait" in call
    assert result.resource_deletions == []
    (record,) = result.record_deletions
    assert record[record.index("--name") + 1] == DEPLOYMENT
    steps = [line for line in result.order if line in ("zia delete", "az group delete", "az deployment sub delete")]
    assert steps == ["zia delete", "az group delete", "az deployment sub delete"]
    # The record is deleted only after the resource group is gone.
    assert result.order.index("az deployment sub delete") > max(
        index for index, line in enumerate(result.order) if line == "az group exists"
    )
    assert f"{GROUP} is DELETED with everything in it, because this deployment created it" in result.output
    assert f"'{SERVER_NAME}' with the ID {SERVER_ID} is DELETED" in result.output
    assert "pending change in ZIA" in result.output
    assert f"The resource group '{GROUP}' is deleted." in result.output
    assert "Destroyed." in result.output


@pytest.mark.parametrize("kind", KINDS)
def test_the_summary_lists_what_the_deployment_created(kind, tmp_path):
    result = destroy(kind, tmp_path)

    for item in OURS:
        assert f"{item['type']}  {item['name']}" in result.output
    assert "resources left" in result.output  # the progress of the deletion


@pytest.mark.parametrize("kind", KINDS)
def test_the_sdk_gets_the_credentials_and_the_recorded_id_and_name(kind, tmp_path):
    result = destroy(kind, tmp_path)

    connect = result.zia[0]
    assert connect["client"] == "oneapi"
    assert connect["config"]["clientId"] == NOT_A_CLIENT_ID and connect["config"]["vanityDomain"] == "example"
    assert connect["config"]["clientSecret"] == sha256(CLIENT_SECRET)


@pytest.mark.parametrize("kind", KINDS)
def test_destroy_prints_no_secret(kind, tmp_path):
    result = destroy(kind, tmp_path)

    assert result.status == 0, result.output
    for secret in SECRETS:
        assert secret not in result.output
        for call in result.calls:
            assert all(secret not in argument for argument in call)


# --------------------------------------------------------------------------------------
# The confirmation
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_nothing_is_deleted_without_the_confirmation(kind, tmp_path):
    # ASSUME_YES=1, which run() sets for deploy.sh, does not confirm a destroy.
    result = destroy(kind, tmp_path, confirm=None)

    assert nothing_was_deleted(result)
    assert result.zia == []
    assert "Nothing was deleted." in result.output
    assert "Destroyed." not in result.output
    if kind == "bash":
        assert result.status == 1
        assert f"CONFIRM_DESTROY={GROUP}" in result.output


@pytest.mark.skipif(not PWSH, reason="PowerShell 7 (pwsh) is not installed")
def test_powershell_yes_does_not_confirm_a_destroy(tmp_path):
    result = destroy("pwsh", tmp_path, confirm=None, arguments=["-Yes"])

    assert "Nothing was deleted." in result.output
    assert nothing_was_deleted(result) and result.zia == []


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("confirm", ["yes", "1", GROUP.lower(), f"{GROUP}-2"])
def test_a_confirmation_that_is_not_the_name_of_the_group_is_refused(kind, confirm, tmp_path):
    result = destroy(kind, tmp_path, confirm=confirm)

    assert result.status == 1
    assert "is not the name of the resource group" in result.output
    assert nothing_was_deleted(result) and result.zia == []


@pytest.mark.parametrize("kind", KINDS)
def test_in_a_terminal_the_name_of_the_group_confirms(kind, tmp_path):
    terminal = destroy_in_terminal(kind, tmp_path, answers=[(CONFIRM_QUESTION, GROUP)])

    assert terminal.status == 0, terminal.text
    assert "This cannot be undone." in terminal.text
    assert len(terminal.group_deletions) == 1 and len(terminal.record_deletions) == 1
    assert terminal.zia_calls == ["connect", "list", "delete"]
    assert "Destroyed." in terminal.text


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("answer", ["y", "yes", "", GROUP.lower()])
def test_in_a_terminal_any_other_answer_deletes_nothing(kind, answer, tmp_path):
    terminal = destroy_in_terminal(kind, tmp_path, answers=[(CONFIRM_QUESTION, answer)])

    assert terminal.status == 0, terminal.text
    assert "Nothing was deleted." in terminal.text
    assert nothing_was_deleted(terminal) and terminal.zia == []


@pytest.mark.parametrize("kind", KINDS)
def test_ctrl_c_at_the_confirmation_deletes_nothing(kind, tmp_path):
    directory = run_directory(tmp_path)
    terminal = Terminal(
        kind, "destroy", directory, destroy_scenario(), environment=destroy_environment(tmp_path, None), location=""
    )
    terminal.read_until(CONFIRM_QUESTION).watch(0.5)
    terminal.press_ctrl_c().finish()
    read_fakes(terminal, tmp_path, terminal.directory)

    assert "Stopped. Nothing was deleted." in terminal.text
    assert nothing_was_deleted(terminal) and terminal.zia == []
    if kind == "bash":
        assert terminal.status == 130


# --------------------------------------------------------------------------------------
# A resource group that holds more than the deployment
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_a_group_with_other_resources_is_kept_and_only_the_deployment_is_deleted(kind, tmp_path):
    result = destroy(kind, tmp_path, destroy_scenario(theirs=THEIRS))

    assert result.status == 0, result.output
    assert result.group_deletions == []
    (call,) = result.resource_deletions
    assert sorted(deleted_ids(call)) == sorted(item["id"] for item in OURS)
    assert len(result.record_deletions) == 1
    assert result.zia_calls == ["connect", "list", "delete"]
    assert f"{GROUP} is kept, because it holds 2 resources this deployment did not create" in result.output
    assert "the 8 resources this deployment created" in result.output
    assert f"{VNET}  Customer-VNET" in result.output and f"{STORAGE}  customerstorage" in result.output
    # The container with the copied VHD is in a storage account that is not the deployment's.
    assert "inside a resource that is not its own" in result.output and VHD_CONTAINER in result.output
    assert ROLE_ASSIGNMENT not in result.output
    assert f"The resource group '{GROUP}' is kept." in result.output


@pytest.mark.parametrize("kind", KINDS)
def test_a_resource_the_deployment_only_read_is_never_deleted(kind, tmp_path):
    result = destroy(kind, tmp_path, destroy_scenario(theirs=THEIRS))

    (call,) = result.resource_deletions
    ids = deleted_ids(call)
    assert rid(STORAGE, "customerstorage") not in ids and rid(VNET, "Customer-VNET") not in ids
    # Neither a part of a resource nor something attached to one is deleted by itself.
    assert VHD_CONTAINER not in ids and ROLE_ASSIGNMENT not in ids
    assert all(len(resource_id.split("/")) == 9 for resource_id in ids)


@pytest.mark.parametrize("kind", KINDS)
def test_the_virtual_machine_and_the_scripts_are_deleted_first(kind, tmp_path):
    result = destroy(kind, tmp_path, destroy_scenario(theirs=THEIRS))

    (call,) = result.resource_deletions
    assert sorted(deleted_ids(call)[:2]) == sorted([rid(VM, "Zscaler-NSS-VM"), rid(SCRIPTS, COPY_SCRIPT)])


@pytest.mark.parametrize("kind", KINDS)
def test_what_a_failed_script_left_behind_is_deleted_with_it(kind, tmp_path):
    left = [resource(CONTAINER_GROUP, "abcazscripts"), resource(STORAGE, "abcazscripts")]
    scripts = {
        GROUP: [
            {
                "id": rid(SCRIPTS, COPY_SCRIPT).upper(),
                "status": {"storageAccountId": left[1]["id"], "containerInstanceId": left[0]["id"]},
                "outputs": None,
            },
            # A deployment script of somebody else, and what it left behind.
            {
                "id": rid(SCRIPTS, "their-script"),
                "status": {"storageAccountId": rid(STORAGE, "theirazscripts"), "containerInstanceId": None},
                "outputs": {"nssServerId": "999"},
            },
        ]
    }
    theirs = [resource(SCRIPTS, "their-script"), resource(STORAGE, "theirazscripts")]
    result = destroy(kind, tmp_path, destroy_scenario(theirs=left + theirs, scripts=scripts))

    assert result.status == 0, result.output
    (call,) = result.resource_deletions
    assert sorted(deleted_ids(call)) == sorted(item["id"] for item in OURS + left)
    assert "it holds 2 resources this deployment did not create" in result.output
    assert result.zia[-1] == {"call": "delete", "id": SERVER_ID}


@pytest.mark.parametrize("kind", KINDS)
def test_what_is_left_after_a_try_is_deleted_in_the_next(kind, tmp_path):
    nsg, disk = rid(NSG, "Zscaler-NSS-VM-NSG"), rid(DISK, "Zscaler-NSS-VM-osdisk")
    result = destroy(kind, tmp_path, destroy_scenario(theirs=THEIRS, stubborn={nsg: 2, disk: 1}))

    assert result.status == 0, result.output
    first, second, third = result.resource_deletions
    assert len(deleted_ids(first)) == 8
    assert sorted(deleted_ids(second)) == sorted([nsg, disk])
    assert deleted_ids(third) == [nsg]
    assert "2 resources left. Trying those again." in result.output
    assert len(result.record_deletions) == 1


@pytest.mark.parametrize("kind", KINDS)
def test_a_resource_azure_does_not_delete_ends_the_destroy_and_keeps_the_record(kind, tmp_path):
    nsg = rid(NSG, "Zscaler-NSS-VM-NSG")
    result = destroy(kind, tmp_path, destroy_scenario(theirs=THEIRS, stubborn={nsg: 99}))

    assert result.status == 1
    assert len(result.resource_deletions) == 2
    assert "Not deleted:" in result.output and f"{NSG}  Zscaler-NSS-VM-NSG" in result.output
    assert "Some resources failed to be deleted" in result.output  # the message of the Azure CLI
    assert result.record_deletions == []
    assert "Destroyed." not in result.output


@pytest.mark.parametrize("kind", KINDS)
def test_a_step_whose_record_cannot_be_read_keeps_what_it_created(kind, tmp_path):
    operations = {name: value for name, value in GROUP_OPERATIONS.items() if name != step("virtualMachine")}
    result = destroy(kind, tmp_path, destroy_scenario(group_operations=operations))

    assert result.status == 0, result.output
    assert result.group_deletions == []
    (call,) = result.resource_deletions
    kept = {rid(VM, "Zscaler-NSS-VM"), rid(DISK, "Zscaler-NSS-VM-osdisk"), rid(NIC, "Zscaler-NSS-VM-MGMT-NIC")}
    assert kept.isdisjoint(deleted_ids(call))
    # The service interface is created by a deployment inside the one that cannot be read.
    assert rid(NIC, "Zscaler-NSS-VM-SRVC-NIC") not in deleted_ids(call)
    assert "the record of these steps cannot be read" in result.output
    assert "virtualMachine" in result.output and step("virtualMachine") not in result.output


# --------------------------------------------------------------------------------------
# A resource group the record does not show as created by the deployment
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_a_group_that_existed_is_kept_where_nothing_can_be_asked(kind, tmp_path):
    result = destroy(kind, tmp_path, destroy_scenario(**EXISTED))

    assert result.status == 0, result.output
    assert result.group_deletions == []
    (call,) = result.resource_deletions
    assert sorted(deleted_ids(call)) == sorted(item["id"] for item in OURS)
    assert "it existed when the deployment last ran" in result.output


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("answer, deleted", [("y", True), ("", False), ("n", False)])
def test_in_a_terminal_a_group_that_existed_is_asked_about(kind, answer, deleted, tmp_path):
    answers = [(GROUP_QUESTION, answer), (CONFIRM_QUESTION, GROUP)]
    terminal = destroy_in_terminal(kind, tmp_path, destroy_scenario(**EXISTED), answers)

    assert terminal.status == 0, terminal.text
    assert "holds nothing but this deployment, but it existed" in terminal.text
    assert bool(terminal.group_deletions) is deleted
    assert bool(terminal.resource_deletions) is not deleted
    assert len(terminal.record_deletions) == 1


@pytest.mark.parametrize("kind", KINDS)
def test_a_group_that_existed_is_deleted_when_that_is_asked_for(kind, tmp_path):
    result = destroy(kind, tmp_path, destroy_scenario(**EXISTED), environment={"KEEP_RESOURCE_GROUP": "0"})

    assert result.status == 0, result.output
    assert len(result.group_deletions) == 1 and result.resource_deletions == []


@pytest.mark.parametrize("kind", KINDS)
def test_a_group_with_other_resources_is_kept_whatever_is_asked_for(kind, tmp_path):
    the_scenario = destroy_scenario(theirs=THEIRS)
    result = destroy(kind, tmp_path, the_scenario, environment={"KEEP_RESOURCE_GROUP": "0"})

    assert result.status == 0, result.output
    assert result.group_deletions == [] and len(result.resource_deletions) == 1


@pytest.mark.parametrize("kind", KINDS)
def test_the_group_the_deployment_created_is_kept_when_that_is_asked_for(kind, tmp_path):
    result = destroy(kind, tmp_path, environment={"KEEP_RESOURCE_GROUP": "1"})

    assert result.status == 0, result.output
    assert result.group_deletions == [] and len(result.resource_deletions) == 1
    assert f"{GROUP} is kept, because KEEP_RESOURCE_GROUP" in result.output


# --------------------------------------------------------------------------------------
# The state of the resource group
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_a_group_that_is_gone_leaves_the_server_and_the_record_to_delete(kind, tmp_path):
    result = destroy(kind, tmp_path, destroy_scenario(groups={}))

    assert result.status == 0, result.output
    assert f"{GROUP} no longer exists" in result.output
    assert result.group_deletions == [] and result.resource_deletions == []
    assert result.zia_calls == ["connect", "list", "delete"]
    assert len(result.record_deletions) == 1


@pytest.mark.parametrize("kind", KINDS)
def test_a_group_that_is_being_deleted_is_waited_for(kind, tmp_path):
    result = destroy(kind, tmp_path, destroy_scenario(deleting_groups=[GROUP], group_delete_polls=2))

    assert result.status == 0, result.output
    # The deletion is not started a second time.
    assert result.group_deletions == []
    assert "because it is already being deleted" in result.output
    assert f"The resource group '{GROUP}' is being deleted." in result.output
    assert result.zia_calls == ["connect", "list", "delete"]
    assert len(result.record_deletions) == 1


@pytest.mark.parametrize("kind", KINDS)
def test_a_group_azure_does_not_delete_ends_the_destroy_and_keeps_the_record(kind, tmp_path):
    result = destroy(kind, tmp_path, destroy_scenario(group_delete_fails=True))

    assert result.status == 1
    assert len(result.group_deletions) == 1
    assert f"Azure did not delete the resource group '{GROUP}'" in result.output
    assert result.record_deletions == []
    assert "Destroyed." not in result.output


@pytest.mark.parametrize("kind", KINDS)
def test_a_deletion_azure_refuses_to_start_keeps_the_record(kind, tmp_path):
    the_scenario = destroy_scenario(group_delete_error="(ScopeLocked) The scope is locked.")
    result = destroy(kind, tmp_path, the_scenario)

    assert result.status == 1
    assert "ScopeLocked" in result.output and "was not started" in result.output
    assert result.record_deletions == []


@pytest.mark.parametrize("kind", KINDS)
def test_ctrl_c_during_the_deletion_says_that_it_continues_and_a_second_run_finishes(kind, tmp_path):
    directory = run_directory(tmp_path)
    the_scenario = destroy_scenario(group_delete_polls=1000)
    terminal = Terminal(
        kind,
        "destroy",
        directory,
        the_scenario,
        environment=destroy_environment(tmp_path, GROUP),
        location="",
        poll_seconds="1",
    )
    terminal.read_until("resources left").watch(1.5)
    terminal.press_ctrl_c().finish()
    read_fakes(terminal, tmp_path, terminal.directory)

    assert f"Stopped watching. The deletion of the resource group '{GROUP}' continues in Azure." in terminal.text
    assert len(terminal.group_deletions) == 1 and terminal.record_deletions == []
    assert "delete" in terminal.zia_calls

    # The second run finds the server gone and the resource group being deleted.
    the_scenario.update(deleting_groups=[GROUP], group_delete_polls=1)
    second = destroy(kind, tmp_path, the_scenario)

    assert second.status == 0, second.output
    assert f"ZIA has no NSS server with the ID {SERVER_ID}" in second.output
    assert second.group_deletions == [] and len(second.record_deletions) == 1
    assert "Destroyed." in second.output


# --------------------------------------------------------------------------------------
# The record of the deployment
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_without_a_record_nothing_is_deleted(kind, tmp_path):
    result = destroy(kind, tmp_path, destroy_scenario(deployment_missing=True))

    assert result.status == 1
    assert f"There is no deployment '{DEPLOYMENT}'" in result.output
    assert nothing_was_deleted(result) and result.zia == []


@pytest.mark.parametrize("kind", KINDS)
def test_a_record_that_cannot_be_read_deletes_nothing(kind, tmp_path):
    result = destroy(kind, tmp_path, destroy_scenario(deployment_unreadable=True))

    assert result.status == 1
    assert "AuthorizationFailed" in result.output and "cannot be read" in result.output
    assert nothing_was_deleted(result) and result.zia == []


@pytest.mark.parametrize("kind", KINDS)
def test_a_deployment_that_is_running_is_not_destroyed(kind, tmp_path):
    result = destroy(kind, tmp_path, destroy_scenario(state="Running"))

    assert result.status == 1
    assert "has not ended" in result.output and "az deployment sub cancel" in result.output
    assert nothing_was_deleted(result) and result.zia == []


@pytest.mark.parametrize("kind", KINDS)
def test_a_record_of_another_resource_group_is_refused(kind, tmp_path):
    the_scenario = destroy_scenario(groups={GROUP: "eastus", "Other-RG": "eastus"})
    environment = {"DEPLOYMENT_NAME": DEPLOYMENT}
    result = destroy(
        kind, tmp_path, the_scenario, resource_group="Other-RG", confirm="Other-RG", environment=environment
    )

    assert result.status == 1
    assert f"went into the resource group '{GROUP}', not into 'Other-RG'" in result.output
    assert nothing_was_deleted(result) and result.zia == []


@pytest.mark.parametrize("kind", KINDS)
def test_the_name_of_the_group_is_taken_as_the_record_has_it(kind, tmp_path):
    result = destroy(kind, tmp_path, resource_group=GROUP.lower())

    assert result.status == 0, result.output
    (call,) = result.group_deletions
    assert call[call.index("--name") + 1] == GROUP


@pytest.mark.parametrize("kind", KINDS)
def test_without_a_resource_group_the_one_of_the_saved_answers_is_destroyed(kind, tmp_path):
    save(tmp_path, [f"RESOURCE_GROUP={GROUP}", "LOCATION=eastus"])
    environment = {"ANSWERS_FILE": str(answers_file(tmp_path))}
    result = destroy(kind, tmp_path, resource_group="", environment=environment)

    assert result.status == 0, result.output
    assert f"Resource group '{GROUP}', from the answers in" in result.output
    assert len(result.group_deletions) == 1
    # The answers are kept for the next deployment.
    assert answers_file(tmp_path).exists()


@pytest.mark.parametrize("kind", KINDS)
def test_a_record_that_cannot_be_deleted_is_reported(kind, tmp_path):
    the_scenario = destroy_scenario(record_delete_error="(AuthorizationFailed) No permission to delete the deployment.")
    result = destroy(kind, tmp_path, the_scenario)

    assert result.status == 1
    assert "was not deleted" in result.output and "Everything else is deleted." in result.output
    assert len(result.group_deletions) == 1


# --------------------------------------------------------------------------------------
# The NSS server object
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_the_server_is_kept_when_that_is_asked_for(kind, tmp_path):
    # Without a Python: none is needed when nothing is deleted in ZIA.
    environment = {"KEEP_NSS_SERVER": "1", "PYTHON": str(tmp_path / "no-such-python")}
    result = destroy(kind, tmp_path, environment=environment)

    assert result.status == 0, result.output
    assert result.zia == []
    assert f"the object with the ID {SERVER_ID} is kept" in result.output
    assert len(result.group_deletions) == 1 and len(result.record_deletions) == 1
    assert "pending change" not in result.output


@pytest.mark.parametrize("kind", KINDS)
def test_a_deployment_without_the_certificate_step_deletes_nothing_in_zia(kind, tmp_path):
    the_scenario = destroy_scenario(parameters=parameters(installNssCertificate=False), outputs={})
    environment = {"PYTHON": str(tmp_path / "no-such-python")}
    result = destroy(kind, tmp_path, the_scenario, environment=environment)

    assert result.status == 0, result.output
    assert result.zia == []
    assert "the deployment did not register the VM in ZIA" in result.output
    assert len(result.group_deletions) == 1


@pytest.mark.parametrize("kind", KINDS)
def test_a_failed_deployment_without_a_recorded_id_deletes_nothing_in_zia(kind, tmp_path):
    result = destroy(kind, tmp_path, destroy_scenario(state="Failed"))

    assert result.status == 0, result.output
    # No server is looked for by its name: one with that name may be in use by another VM.
    assert result.zia == []
    assert "the deployment recorded no NSS server ID" in result.output
    assert len(result.group_deletions) == 1 and len(result.record_deletions) == 1


@pytest.mark.parametrize("kind", KINDS)
def test_a_failed_deployment_takes_the_id_from_its_certificate_script(kind, tmp_path):
    scripts = {
        GROUP: [
            {"id": rid(SCRIPTS, COPY_SCRIPT), "status": {}, "outputs": None},
            {"id": rid(SCRIPTS, CERTIFICATE_SCRIPT), "status": {}, "outputs": {"nssServerId": str(SERVER_ID)}},
        ]
    }
    operations = dict(GROUP_OPERATIONS)
    operations[NESTED] = operations[NESTED] + [created(DEPLOYMENTS, step("nssCertificate"))]
    operations[step("nssCertificate")] = [created(SCRIPTS, CERTIFICATE_SCRIPT)]
    the_scenario = destroy_scenario(
        state="Failed",
        scripts=scripts,
        group_operations=operations,
        resources={GROUP: OURS + [resource(SCRIPTS, CERTIFICATE_SCRIPT)]},
    )
    result = destroy(kind, tmp_path, the_scenario)

    assert result.status == 0, result.output
    assert result.zia[-1] == {"call": "delete", "id": SERVER_ID}
    assert f"'{SERVER_NAME}' with the ID {SERVER_ID} is DELETED" in result.output


@pytest.mark.parametrize("kind", KINDS)
def test_a_failed_deletion_in_zia_deletes_nothing_in_azure(kind, tmp_path):
    (tmp_path / "zia").mkdir()
    (tmp_path / "zia" / "delete_error").write_text("NSS server is referenced by an NSS feed", encoding="utf-8")
    result = destroy(kind, tmp_path)

    assert result.status == 1
    assert "NSS server is referenced by an NSS feed" in result.output
    assert "nothing was deleted in Azure either" in result.output
    assert result.group_deletions == [] and result.resource_deletions == [] and result.record_deletions == []
    assert result.servers == SERVERS


@pytest.mark.parametrize("kind", KINDS)
def test_a_server_with_another_name_is_not_deleted_and_nothing_in_azure(kind, tmp_path):
    servers = [{"id": SERVER_ID, "name": "Renamed", "state": "HEALTHY"}]
    result = destroy(kind, tmp_path, servers=servers)

    assert result.status == 1
    assert f"is named 'Renamed' in ZIA, not '{SERVER_NAME}'" in result.output
    assert result.zia_calls == ["connect", "list"]
    assert result.group_deletions == [] and result.record_deletions == []


@pytest.mark.parametrize("kind", KINDS)
def test_a_given_id_is_deleted_whatever_its_name(kind, tmp_path):
    servers = [{"id": 777, "name": "Renamed", "state": "HEALTHY"}, SERVERS[0]]
    result = destroy(kind, tmp_path, servers=servers, environment={"NSS_SERVER_ID": "777"})

    assert result.status == 0, result.output
    assert result.zia[-1] == {"call": "delete", "id": 777}
    assert result.servers == [SERVERS[0]]
    assert "the object with the ID 777, from NSS_SERVER_ID, is DELETED" in result.output


@pytest.mark.parametrize("kind", KINDS)
def test_a_given_id_that_is_not_a_number_is_refused(kind, tmp_path):
    result = destroy(kind, tmp_path, environment={"NSS_SERVER_ID": "42; rm -rf /"})

    assert result.status == 1
    assert "which is a number" in result.output
    assert nothing_was_deleted(result) and result.zia == []


@pytest.mark.parametrize("kind", KINDS)
def test_a_server_that_is_already_gone_does_not_stop_the_destroy(kind, tmp_path):
    result = destroy(kind, tmp_path, servers=SERVERS[1:])

    assert result.status == 0, result.output
    assert result.zia_calls == ["connect", "list"]
    assert f"ZIA has no NSS server with the ID {SERVER_ID}" in result.output
    assert len(result.group_deletions) == 1 and len(result.record_deletions) == 1


@pytest.mark.parametrize("kind", KINDS)
def test_without_credentials_nothing_is_deleted_where_nothing_can_be_asked(kind, tmp_path):
    environment = {"ZSCALER_CLIENT_ID": None, "ZSCALER_CLIENT_SECRET": None}
    result = destroy(kind, tmp_path, environment=environment)

    assert result.status == 1
    assert "neither set of Zscaler API variables is complete" in result.output
    assert "KEEP_NSS_SERVER" in result.output or "-KeepNssServer" in result.output
    assert nothing_was_deleted(result) and result.zia == []


@pytest.mark.parametrize("kind", KINDS)
def test_in_a_terminal_the_credentials_are_asked_for_and_not_shown(kind, tmp_path):
    # What a deployment that asked its questions saved: everything but the secrets.
    save(
        tmp_path,
        [f"RESOURCE_GROUP={GROUP}", "ZSCALER_API=oneapi", "ZSCALER_VANITY_DOMAIN=saved-domain", "ZSCALER_CLOUD="],
    )
    before = answers_file(tmp_path).read_text(encoding="utf-8")
    environment = {
        "ANSWERS_FILE": str(answers_file(tmp_path)),
        "ZSCALER_CLIENT_ID": None,
        "ZSCALER_CLIENT_SECRET": None,
        "ZSCALER_VANITY_DOMAIN": None,
    }
    answers = [
        ("OneAPI client ID", NOT_A_TYPED_CLIENT_ID),
        ("OneAPI client secret", CLIENT_SECRET),
        (CONFIRM_QUESTION, GROUP),
    ]
    terminal = destroy_in_terminal(kind, tmp_path, answers=answers, environment=environment)

    assert terminal.status == 0, terminal.text
    connect = terminal.zia[0]["config"]
    # The domain is the one of the saved answers; the kind of API is not asked for again.
    assert connect["vanityDomain"] == "saved-domain" and connect["clientId"] == NOT_A_TYPED_CLIENT_ID
    assert connect["clientSecret"] == sha256(CLIENT_SECRET)
    assert "Choose 1-2" not in terminal.text
    assert CLIENT_SECRET not in terminal.text and NOT_A_TYPED_CLIENT_ID not in terminal.text
    assert terminal.zia_calls == ["connect", "list", "delete"]
    assert answers_file(tmp_path).read_text(encoding="utf-8") == before


@pytest.mark.parametrize("kind", KINDS)
def test_ctrl_c_at_a_credential_deletes_nothing(kind, tmp_path):
    environment = destroy_environment(
        tmp_path, None, environment={"ZSCALER_CLIENT_SECRET": None, "ANSWERS_FILE": str(answers_file(tmp_path))}
    )
    terminal = Terminal(
        kind, "destroy", run_directory(tmp_path), destroy_scenario(), environment=environment, location=""
    )
    # Without saved answers, the cloud is asked for: the credentials of another cloud need it.
    terminal.answer(CLOUD, "")
    terminal.read_until("OneAPI client secret").watch(0.5)
    terminal.press_ctrl_c().finish()
    read_fakes(terminal, tmp_path, terminal.directory)

    assert "Stopped. Nothing was deleted." in terminal.text
    assert nothing_was_deleted(terminal) and terminal.zia == []


# --------------------------------------------------------------------------------------
# The Python that deletes the NSS server object
# --------------------------------------------------------------------------------------


def stub_python(tmp_path, *, version_ok=True, pip_fails=False):
    """A Python without zscaler-sdk-python, whose virtual environment has the stand-in for it."""
    log = tmp_path / "python.log"
    in_environment = tmp_path / "python-in-the-environment"
    in_environment.write_text(
        "#!/bin/sh\n"
        f'echo "environment: $*" >> "{log}"\n'
        'if [ "$1 $2" = "-m pip" ]; then\n'
        + ('  echo "ERROR: No matching distribution found" >&2\n  exit 1\n' if pip_fails else "  exit 0\n")
        + "fi\n"
        f'PYTHONPATH="{REPOSITORY / "tests" / "fake_zscaler"}" exec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    in_environment.chmod(0o755)
    python = tmp_path / "python-without-the-sdk"
    python.write_text(
        "#!/bin/sh\n"
        f'echo "python: $*" >> "{log}"\n'
        'case "$1 $2" in\n'
        f'  "-c import sys"*) exit {0 if version_ok else 1} ;;\n'
        '  "-c import zscaler") exit 1 ;;\n'
        f'  "-m venv") mkdir -p "$3/bin" && cp "{in_environment}" "$3/bin/python" ;;\n'
        "  *) exit 64 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    python.chmod(0o755)
    return python, log


@pytest.mark.parametrize("kind", KINDS)
def test_a_python_without_the_sdk_gets_it_in_a_temporary_environment(kind, tmp_path):
    python, log = stub_python(tmp_path)
    result = destroy(kind, tmp_path, environment={"PYTHON": str(python)})

    assert result.status == 0, result.output
    lines = log.read_text(encoding="utf-8").splitlines()
    venv = next(line for line in lines if line.startswith("python: -m venv "))
    directory = venv.split(" ", 3)[3]
    assert directory.startswith(str(tmp_path))  # TMPDIR of the test
    install = next(line for line in lines if line.startswith("environment: -m pip install"))
    assert install.endswith(" zscaler-sdk-python==1.11.5")
    assert "Installing zscaler-sdk-python==1.11.5 into a temporary Python environment" in result.output
    assert result.zia_calls == ["connect", "list", "delete"]
    assert len(result.group_deletions) == 1
    # Nothing is installed before the confirmation, and the environment is removed afterwards.
    assert not Path(directory).exists()


@pytest.mark.parametrize("kind", KINDS)
def test_nothing_is_installed_before_the_confirmation(kind, tmp_path):
    python, log = stub_python(tmp_path)
    result = destroy(kind, tmp_path, confirm=None, environment={"PYTHON": str(python)})

    assert nothing_was_deleted(result)
    assert "venv" not in log.read_text(encoding="utf-8")


@pytest.mark.parametrize("kind", KINDS)
def test_an_sdk_that_cannot_be_installed_deletes_nothing(kind, tmp_path):
    python, _ = stub_python(tmp_path, pip_fails=True)
    result = destroy(kind, tmp_path, environment={"PYTHON": str(python)})

    assert result.status == 1
    assert "No matching distribution found" in result.output and "could not be installed" in result.output
    assert nothing_was_deleted(result) and result.zia == []


@pytest.mark.parametrize("kind", KINDS)
def test_a_python_that_is_too_old_is_refused_before_the_confirmation(kind, tmp_path):
    python, _ = stub_python(tmp_path, version_ok=False)
    result = destroy(kind, tmp_path, environment={"PYTHON": str(python)})

    assert result.status == 1
    assert "is not Python 3.10 or later" in result.output
    assert "This cannot be undone." not in result.output
    assert nothing_was_deleted(result) and result.zia == []


# --------------------------------------------------------------------------------------
# The Makefile
# --------------------------------------------------------------------------------------


@pytest.mark.skipif(not (BASH and MAKE), reason="make or bash is not installed")
def test_make_destroy_passes_its_variables_to_the_script(tmp_path):
    result = destroy("bash", tmp_path, environment={"KEEP_NSS_SERVER": "1"}, through_make=True)

    assert result.status == 0, result.output
    assert len(result.group_deletions) == 1 and result.zia == []
    assert "Destroyed." in result.output


@pytest.mark.skipif(not MAKE, reason="make is not installed")
def test_the_help_of_make_names_destroy():
    completed = subprocess.run(
        [MAKE, "--no-print-directory", "-C", str(REPOSITORY)], capture_output=True, text=True, timeout=60
    )

    assert "make destroy" in completed.stdout and "CONFIRM_DESTROY" in completed.stdout
