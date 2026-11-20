"""A stand-in for the Azure CLI, used by test_deploy_scripts.py.

The tests put an executable named ``az`` that runs this file first on the PATH. It answers the
commands scripts/deploy.sh and scripts/deploy.ps1 run, from a scenario file, and writes every
command line it receives to a file. It never contacts Azure.

The directory in FAKE_AZ_DIR holds:
    scenario.json  what the fake subscription contains and how the deployment proceeds
    calls.jsonl    one JSON list of arguments per call, appended by this file
    environment.jsonl  per call, the SHA-256 of every NSS_*, ZSCALER_* and ZIA_* variable that
                   is set, so a test can tell which value a call received without the value
                   being written anywhere
    polls          how many times the state of the deployment was read
    state.json     what the commands of a destroy have deleted so far

The deployment proceeds through the "phases" of the scenario: every read of the deployment
state moves to the next phase, and the last phase stays. The --query expressions are evaluated
with the same library the Azure CLI uses, so a wrong expression in a script fails here too.

For a destroy the scenario also holds:
    parameters     the parameters in the record of the deployment
    resources      per resource group, its resources as {"id", "type", "name"}
    scripts        per resource group, what 'deployment-scripts list' returns
    group_delete_polls   how many times a resource group that is being deleted still exists
    group_delete_fails   true: the resource group is back in the state Succeeded after that
    deleting_groups      resource groups that are being deleted when the scenario starts. With
                         group_delete_polls they are gone after that many times, without it never
    stubborn       per resource ID, in how many 'resource delete' commands it is not deleted
A resource group is deleted with --no-wait and is gone after it was asked for
group_delete_polls times. 'resource delete' deletes the IDs it is given, except the stubborn
ones, and fails when one of them is left.
"""

import hashlib
import json
import os
import sys
from pathlib import Path

import jmespath


def option(arguments, *names):
    for index, argument in enumerate(arguments):
        if argument in names and index + 1 < len(arguments):
            return arguments[index + 1]
    return None


def fail(message, status=1):
    sys.stderr.write(f"ERROR: {message}\n")
    sys.exit(status)


def tsv_value(value):
    if isinstance(value, list):
        return str(len(value))
    if isinstance(value, dict):
        return ""
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def tsv_row(row):
    if isinstance(row, dict):
        row = [value for _, value in sorted(row.items())]
    if isinstance(row, list):
        return "\t".join(tsv_value(value) for value in row)
    return tsv_value(row)


def write(result, output):
    if result is None:
        return
    if output == "tsv":
        rows = result if isinstance(result, list) else [result]
        sys.stdout.write("".join(tsv_row(row) + "\n" for row in rows))
    elif output == "yaml" and isinstance(result, dict):
        sys.stdout.write("".join(f"{key}: {json.dumps(value)}\n" for key, value in result.items()))
    else:
        sys.stdout.write(json.dumps(result, indent=2) + "\n")


def options(arguments, name):
    """The values that follow an option, up to the next option."""
    values = []
    if name in arguments:
        for argument in arguments[arguments.index(name) + 1 :]:
            if argument.startswith("-"):
                break
            values.append(argument)
    return values


def group_of(scenario, state, name):
    """The location of a resource group that exists, whatever the case of its name."""
    for group, location in scenario.get("groups", {}).items():
        if group.lower() == (name or "").lower() and group not in state["deleted_groups"]:
            return group, location
    return None, None


def main():
    arguments = sys.argv[1:]
    directory = Path(os.environ["FAKE_AZ_DIR"])
    scenario = json.loads((directory / "scenario.json").read_text(encoding="utf-8"))
    with (directory / "calls.jsonl").open("a", encoding="utf-8") as calls:
        calls.write(json.dumps(arguments) + "\n")
    with (directory / "environment.jsonl").open("a", encoding="utf-8") as variables:
        received = {
            name: hashlib.sha256(value.encode()).hexdigest()
            for name, value in sorted(os.environ.items())
            if name.startswith(("NSS_", "ZSCALER_", "ZIA_")) and value
        }
        variables.write(json.dumps(received) + "\n")

    if "--only-show-errors" not in arguments:
        sys.stderr.write("WARNING: a warning the scripts are expected to suppress\n")

    command = []
    for argument in arguments:
        if argument.startswith("-"):
            break
        command.append(argument)
    command = " ".join(command)
    # tests/fake_zscaler writes to the same file, which gives the order of the two.
    with (directory / "order").open("a", encoding="utf-8") as order:
        order.write(f"az {command}\n")
    name = option(arguments, "--name", "-n")
    phases = scenario.get("phases") or [{}]
    polls_file = directory / "polls"
    polls = int(polls_file.read_text()) if polls_file.exists() else 0
    phase = phases[min(max(polls - 1, 0), len(phases) - 1)]
    state_file = directory / "state.json"
    state = {
        "deleted_groups": [],
        "deleting_groups": {},
        "finished_groups": [],
        "deleted_resources": [],
        "stubborn": {},
        "record_deleted": False,
    }
    if state_file.exists():
        state = json.loads(state_file.read_text(encoding="utf-8"))
    resource_group = option(arguments, "--resource-group", "-g")

    if command == "account show":
        if not scenario.get("account"):
            fail("Please run 'az login' to setup account.")
        result = scenario["account"]
    elif command == "account list-locations":
        if scenario.get("locations") is None:
            fail("the fake subscription cannot list its locations")
        result = [
            {"name": location, "metadata": {"regionType": "Logical" if location.endswith("stage") else "Physical"}}
            for location in scenario["locations"]
        ]
    elif command == "group exists":
        group, _ = group_of(scenario, state, name)
        if (
            group in scenario.get("deleting_groups", [])
            and "group_delete_polls" in scenario
            and group not in state["deleting_groups"]
            and group not in state["finished_groups"]
        ):
            state["deleting_groups"][group] = scenario["group_delete_polls"]
        if group in state["deleting_groups"]:
            state["deleting_groups"][group] -= 1
            if state["deleting_groups"][group] < 0:
                del state["deleting_groups"][group]
                state["finished_groups"].append(group)
                if not scenario.get("group_delete_fails"):
                    state["deleted_groups"].append(group)
                    group = None
        result = group is not None
    elif command == "group show":
        group, location = group_of(scenario, state, name)
        if group is None:
            fail(f"(ResourceGroupNotFound) Resource group '{name}' could not be found.", 3)
        deleting = group in state["deleting_groups"] or (
            group in scenario.get("deleting_groups", []) and group not in state["finished_groups"]
        )
        result = {
            "name": group,
            "location": location,
            "properties": {"provisioningState": "Deleting" if deleting else "Succeeded"},
        }
    elif command == "group delete":
        if "--yes" not in arguments or "--no-wait" not in arguments:
            fail("the fake expects a resource group to be deleted with --yes and --no-wait")
        group, _ = group_of(scenario, state, name)
        if group is None:
            fail(f"(ResourceGroupNotFound) Resource group '{name}' could not be found.", 3)
        if scenario.get("group_delete_error"):
            fail(scenario["group_delete_error"])
        state["deleting_groups"][group] = scenario.get("group_delete_polls", 1)
        result = None
    elif command == "resource list":
        group, _ = group_of(scenario, state, resource_group)
        if group is None:
            fail(f"(ResourceGroupNotFound) Resource group '{resource_group}' could not be found.", 3)
        result = [
            resource
            for resource in scenario.get("resources", {}).get(group, [])
            if resource["id"] not in state["deleted_resources"]
        ]
        # A resource group that is being deleted loses a resource with every look at it.
        if group in state["deleting_groups"]:
            result = result[: state["deleting_groups"][group] + 1]
    elif command == "resource delete":
        ids = options(arguments, "--ids")
        if not ids:
            fail("the fake expects the resources to be deleted by their IDs")
        left = []
        for resource_id in ids:
            remaining = state["stubborn"].get(resource_id, scenario.get("stubborn", {}).get(resource_id, 0))
            if remaining:
                state["stubborn"][resource_id] = remaining - 1
                left.append(resource_id)
            else:
                state["deleted_resources"].append(resource_id)
        state_file.write_text(json.dumps(state), encoding="utf-8")
        if left:
            fail("Some resources failed to be deleted (run with `--verbose` for more information):\n" + "\n".join(left))
        result = None
    elif command == "deployment-scripts list":
        group, _ = group_of(scenario, state, resource_group)
        if scenario.get("scripts") is None or group is None:
            fail("the fake subscription cannot list its deployment scripts")
        result = [
            script for script in scenario["scripts"].get(group, []) if script["id"] not in state["deleted_resources"]
        ]
    elif command == "deployment sub what-if":
        sys.stdout.write("Resource changes: 18 to create.\n")
        return
    elif command == "deployment sub create":
        if "--no-wait" not in arguments:
            fail("the fake expects the deployment to be started with --no-wait")
        if scenario.get("create_error"):
            fail(scenario["create_error"])
        return
    elif command == "deployment sub delete":
        if scenario.get("deployment_missing") or state["record_deleted"]:
            fail(f"(DeploymentNotFound) Deployment '{name}' could not be found.", 3)
        if scenario.get("record_delete_error"):
            fail(scenario["record_delete_error"])
        state["record_deleted"] = True
        result = None
    elif command == "deployment sub show":
        if scenario.get("deployment_missing") or state["record_deleted"]:
            fail(f"(DeploymentNotFound) Deployment '{name}' could not be found.", 3)
        if scenario.get("deployment_unreadable"):
            fail("(AuthorizationFailed) The client does not have authorization to read the deployment.")
        polls += 1
        polls_file.write_text(str(polls))
        phase = phases[min(polls - 1, len(phases) - 1)]
        finished = phase.get("state") in ("Succeeded", "Failed", "Canceled")
        result = {
            "name": name,
            "properties": {
                "provisioningState": phase.get("state", "Running"),
                "parameters": scenario.get("parameters"),
                "outputs": scenario.get("outputs") if phase.get("state") == "Succeeded" else None,
                "error": scenario.get("error") if finished and phase.get("state") != "Succeeded" else None,
            },
        }
    elif command == "deployment operation sub list":
        result = phase.get("subscription_operations", [])
    elif command == "deployment operation group list":
        operations = phase.get("group_operations", {})
        if name not in operations:
            fail(f"(DeploymentNotFound) Deployment '{name}' could not be found.", 3)
        result = operations[name]
    elif command == "deployment-scripts show-log":
        logs = phase.get("logs", {})
        if name not in logs:
            fail(f"(ResourceNotFound) The deployment script '{name}' was not found.", 3)
        result = {"value": [{"name": "default", "log": logs[name]}]}
    else:
        fail(f"the fake Azure CLI does not know the command '{command}'", 2)

    state_file.write_text(json.dumps(state), encoding="utf-8")
    query = option(arguments, "--query")
    if query:
        result = jmespath.search(query, result)
    write(result, option(arguments, "--output", "-o") or "json")


if __name__ == "__main__":
    main()
