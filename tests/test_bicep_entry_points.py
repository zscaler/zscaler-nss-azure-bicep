"""Checks that main.sub.bicep, the subscription-level entry point, stays in step with main.bicep.

main.sub.bicep takes care of the resource group and passes everything else on to main.bicep, so
it declares the parameters and outputs of main.bicep a second time. These tests read the two files
as text and fail when one of them changes without the other. They do not need the Bicep CLI.
"""

import re
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parent.parent
MAIN = (REPOSITORY / "main.bicep").read_text(encoding="utf-8")
SUBSCRIPTION = (REPOSITORY / "main.sub.bicep").read_text(encoding="utf-8")

RESOURCE_GROUP_PARAMETERS = ["existingResourceGroupName", "newResourceGroupName"]
RESOURCE_GROUP_OUTPUTS = ["resourceGroupName", "resourceGroupCreated"]

# Parameters whose description and default differ on purpose: at subscription scope there is no
# resource group to take a default from.
DIFFERENT_ON_PURPOSE = {"location", "virtualNetworkResourceGroup"}

DECLARATION = re.compile(r"((?:^@.*\n(?:^(?!@|param |output ).*\n)*)*)^(param|output) (\w+) ([^\n]*)\n", re.MULTILINE)


def declarations(source, keyword):
    """Returns {name: (decorators, rest of the declaration line)} in the order of the file."""
    return {
        name: (decorators, rest) for decorators, found, name, rest in DECLARATION.findall(source) if found == keyword
    }


MAIN_PARAMETERS = declarations(MAIN, "param")
SUBSCRIPTION_PARAMETERS = declarations(SUBSCRIPTION, "param")
MAIN_OUTPUTS = declarations(MAIN, "output")
SUBSCRIPTION_OUTPUTS = declarations(SUBSCRIPTION, "output")


def test_the_files_are_read_completely():
    assert len(MAIN_PARAMETERS) == len(re.findall(r"^param ", MAIN, re.MULTILINE)) > 30
    assert len(MAIN_OUTPUTS) == len(re.findall(r"^output ", MAIN, re.MULTILINE)) >= 10


def test_the_subscription_file_declares_the_same_parameters_in_the_same_order():
    assert list(SUBSCRIPTION_PARAMETERS) == RESOURCE_GROUP_PARAMETERS + list(MAIN_PARAMETERS)


@pytest.mark.parametrize("name", [name for name in MAIN_PARAMETERS if name not in DIFFERENT_ON_PURPOSE])
def test_a_parameter_has_the_same_decorators_type_and_default(name):
    assert SUBSCRIPTION_PARAMETERS[name] == MAIN_PARAMETERS[name]


@pytest.mark.parametrize("name", sorted(DIFFERENT_ON_PURPOSE))
def test_a_parameter_that_differs_on_purpose_keeps_its_type(name):
    assert SUBSCRIPTION_PARAMETERS[name][1].split(" ")[0] == MAIN_PARAMETERS[name][1].split(" ")[0] == "string"
    assert "resourceGroup()" not in SUBSCRIPTION_PARAMETERS[name][1]


@pytest.mark.parametrize("name", list(MAIN_PARAMETERS))
def test_every_parameter_is_passed_to_main(name):
    module = SUBSCRIPTION[SUBSCRIPTION.index("module nss 'main.bicep'") :]
    arguments = module[module.index("params: {") : module.index("dependsOn")]
    assert re.search(rf"^    {name}: ", arguments, re.MULTILINE)
    if name != "virtualNetworkResourceGroup":
        assert re.search(rf"^    {name}: {name}$", arguments, re.MULTILINE)


def test_the_subscription_file_returns_the_outputs_of_main():
    assert list(SUBSCRIPTION_OUTPUTS) == RESOURCE_GROUP_OUTPUTS + list(MAIN_OUTPUTS)
    for name, (decorators, rest) in MAIN_OUTPUTS.items():
        assert SUBSCRIPTION_OUTPUTS[name][0] == decorators
        assert SUBSCRIPTION_OUTPUTS[name][1] == f"{rest.split(' ')[0]} = nss.outputs.{name}"


def test_an_existing_resource_group_is_never_created_or_changed():
    resources = re.findall(r"^resource (\w+) '([^'@]+)@[^']+' = (.*)\{$", SUBSCRIPTION, re.MULTILINE)
    assert resources == [("newResourceGroup", "Microsoft.Resources/resourceGroups", "if (createResourceGroup) ")]
    assert "var createResourceGroup = empty(existingResourceGroupName)" in SUBSCRIPTION


def test_a_deployment_without_a_resource_group_name_is_refused():
    assert re.search(r"empty\(newResourceGroupName\)\s*\? fail\('", SUBSCRIPTION)


def test_the_parameter_file_of_the_subscription_file_holds_no_secret():
    parameters = (REPOSITORY / "main.sub.bicepparam").read_text(encoding="utf-8")
    assert parameters.startswith("using './main.sub.bicep'")
    secure = [name for name, (decorators, _) in SUBSCRIPTION_PARAMETERS.items() if "@secure()" in decorators]
    assert len(secure) >= 6
    for name in secure:
        assigned = re.search(rf"^param {name} = (.*)$", parameters, re.MULTILINE)
        assert assigned and assigned.group(1).startswith("readEnvironmentVariable('")
