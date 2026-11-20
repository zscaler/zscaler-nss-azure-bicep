"""Tests for scripts/deploy.sh (with the Makefile) and scripts/deploy.ps1.

Both scripts are run against tests/fake_az.py, a stand-in for the Azure CLI that answers from a
scenario and records every command line. Nothing here contacts Azure. The tests prove what the
scripts ask the Azure CLI to do and what they print for the answers; they do not prove that
Azure answers the way the scenarios assume.

They need the jmespath package, bash for deploy.sh and PowerShell 7 (pwsh) for deploy.ps1, and
are skipped without them.
"""

import codecs
import hashlib
import json
import os
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from non_secrets import (
    NOT_A_CLIENT_ID,
    NOT_A_SAS_TOKEN,
    NOT_A_SECRET,
    NOT_A_TYPED_CLIENT_ID,
    NOT_A_TYPED_CLIENT_SECRET,
    NOT_A_TYPED_SAS,
    NOT_A_TYPED_VM_PASSWORD,
    NOT_A_TYPED_ZIA_API_KEY,
    NOT_A_TYPED_ZIA_PASSWORD,
)

pytest.importorskip("jmespath")
if os.name == "nt":
    pytest.skip("the fake Azure CLI of these tests is started through a POSIX shell", allow_module_level=True)

REPOSITORY = Path(__file__).resolve().parent.parent
BASH = "/bin/bash" if Path("/bin/bash").exists() else shutil.which("bash")
PWSH = shutil.which("pwsh")
MAKE = shutil.which("make")

KINDS = [
    pytest.param("bash", marks=pytest.mark.skipif(not BASH, reason="bash is not installed")),
    pytest.param("pwsh", marks=pytest.mark.skipif(not PWSH, reason="PowerShell 7 (pwsh) is not installed")),
]

SAS_TOKEN = NOT_A_SAS_TOKEN
VM_PASSWORD = NOT_A_SECRET
CLIENT_SECRET = NOT_A_SECRET
SECRETS = [SAS_TOKEN, VM_PASSWORD, CLIENT_SECRET, NOT_A_TYPED_CLIENT_SECRET]

GROUP = "NSS-RG"
NESTED = "nss-aaaaaaaaaaaaa"
DEPLOYMENTS = "Microsoft.Resources/deployments"
SCRIPTS = "Microsoft.Resources/deploymentScripts"


def operation(resource_type, name, state, group=None, error=None):
    return {
        "operationId": name,
        "properties": {
            "provisioningState": state,
            "targetResource": {"resourceType": resource_type, "resourceName": name, "resourceGroup": group},
            "statusMessage": {"status": state, "error": error} if error else None,
        },
    }


# An operation without a target resource, as Azure returns for the deployment itself.
BOOKKEEPING = {"operationId": "x", "properties": {"provisioningState": "Succeeded", "targetResource": None}}


def phase(state, nested_state, steps, resources, logs=None):
    """One answer of the fake: the modules of main.bicep and the resources inside each of them."""
    return {
        "state": state,
        "subscription_operations": [
            operation("Microsoft.Resources/resourceGroups", GROUP, "Succeeded"),
            operation(DEPLOYMENTS, NESTED, nested_state, group=GROUP),
            BOOKKEEPING,
        ],
        "group_operations": {
            NESTED: [operation(DEPLOYMENTS, f"{step}-bbbbbbbbbbbbb", step_state) for step, step_state in steps.items()],
            **{f"{step}-bbbbbbbbbbbbb": operations for step, operations in resources.items()},
        },
        "logs": logs or {},
    }


NSG = "Microsoft.Network/networkSecurityGroups"
COPY_SCRIPT_NAME = "Zscaler-NSS-VM-vhd-copy"
COPY_SCRIPT = operation(SCRIPTS, COPY_SCRIPT_NAME, "Succeeded")
# The lines of the copy script, with numbers of a real copy. The last two polls of a real
# copy report the same progress three times.
VHD_BYTES = "536,870,912,512"
COPY_LOG_1 = f"Starting the copy.\nVHD copy Pending: 81,939,922,944 of {VHD_BYTES} bytes copied.\n"
COPY_LOG_2 = COPY_LOG_1 + f"VHD copy Pending: 525,982,498,816 of {VHD_BYTES} bytes copied.\n"
COPY_LOG_3 = COPY_LOG_2 + f"VHD copy Pending: {VHD_BYTES} of {VHD_BYTES} bytes copied.\n"
COPY_LOG_4 = (
    COPY_LOG_3
    + f"VHD copy Pending: {VHD_BYTES} of {VHD_BYTES} bytes copied.\n"
    + f"VHD copy Success: {VHD_BYTES} of {VHD_BYTES} bytes copied.\n"
    + "The VHD has been copied successfully.\n"
)

SUCCESSFUL_PHASES = [
    phase(
        "Running",
        "Running",
        {"networkSecurityGroup": "Running"},
        {"networkSecurityGroup": [operation(NSG, "Zscaler-NSS-VM-NSG", "Running")]},
    ),
    phase(
        "Running",
        "Running",
        {"networkSecurityGroup": "Succeeded", "vhdCopy": "Running"},
        {
            "networkSecurityGroup": [operation(NSG, "Zscaler-NSS-VM-NSG", "Succeeded")],
            "vhdCopy": [operation(SCRIPTS, "Zscaler-NSS-VM-vhd-copy", "Running")],
        },
        {"Zscaler-NSS-VM-vhd-copy": COPY_LOG_1},
    ),
    phase(
        "Running",
        "Running",
        {"networkSecurityGroup": "Succeeded", "vhdCopy": "Running"},
        {
            "networkSecurityGroup": [operation(NSG, "Zscaler-NSS-VM-NSG", "Succeeded")],
            "vhdCopy": [operation(SCRIPTS, "Zscaler-NSS-VM-vhd-copy", "Running")],
        },
        {"Zscaler-NSS-VM-vhd-copy": COPY_LOG_2},
    ),
    phase(
        "Running",
        "Running",
        {"networkSecurityGroup": "Succeeded", "vhdCopy": "Running"},
        {
            "networkSecurityGroup": [operation(NSG, "Zscaler-NSS-VM-NSG", "Succeeded")],
            "vhdCopy": [operation(SCRIPTS, "Zscaler-NSS-VM-vhd-copy", "Running")],
        },
        {"Zscaler-NSS-VM-vhd-copy": COPY_LOG_3},
    ),
    phase(
        "Succeeded",
        "Succeeded",
        {"networkSecurityGroup": "Succeeded", "vhdCopy": "Succeeded", "virtualMachine": "Succeeded"},
        {
            "networkSecurityGroup": [operation(NSG, "Zscaler-NSS-VM-NSG", "Succeeded")],
            "vhdCopy": [operation(SCRIPTS, "Zscaler-NSS-VM-vhd-copy", "Succeeded")],
            "virtualMachine": [operation("Microsoft.Compute/virtualMachines", "Zscaler-NSS-VM", "Succeeded")],
        },
        {"Zscaler-NSS-VM-vhd-copy": COPY_LOG_4},
    ),
]

OUTPUTS = {
    name: {"type": "String", "value": value}
    for name, value in {
        "resourceGroupName": GROUP,
        "vmName": "Zscaler-NSS-VM",
        "managementPublicIpAddress": "203.0.113.20",
        "managementPrivateIpAddress": "192.168.100.132",
        "serviceInterfaceAddress": "192.168.100.4/25",
        "serviceInterfaceGateway": "192.168.100.1",
        "servicePublicIp": "203.0.113.21",
        "storageAccountName": "vhdstorageabc",
        "virtualNetworkName": "Zscaler-NSS-VNET",
        "nssServerId": "4242",
    }.items()
}
OUTPUTS["nssRunning"] = {"type": "Bool", "value": True}
NSS_NOT_RUNNING = "nssRunning is false: NSS is not running on the virtual machine."

SCRIPT_ERROR = {
    "code": "DeploymentScriptError",
    "message": "The provided script failed with the following error:\nERROR: The NSS VM did not answer on port 22.\n\n",
}
FAILED_PHASES = [
    SUCCESSFUL_PHASES[0],
    phase(
        "Failed",
        "Failed",
        {"networkSecurityGroup": "Succeeded", "nssCertificate": "Failed"},
        {
            "networkSecurityGroup": [operation(NSG, "Zscaler-NSS-VM-NSG", "Succeeded")],
            "nssCertificate": [operation(SCRIPTS, "Zscaler-NSS-VM-nss-certificate", "Failed", error=SCRIPT_ERROR)],
        },
        {"Zscaler-NSS-VM-nss-certificate": "Connecting to the NSS VM.\nNo answer after 15 minutes.\n"},
    ),
]


def scenario(**changes):
    base = {
        "account": {"name": "Test Subscription", "id": "00000000-0000-0000-0000-000000000000"},
        "groups": {"Existing-RG": "westeurope"},
        "phases": SUCCESSFUL_PHASES,
        "outputs": OUTPUTS,
        "error": {"code": "DeploymentFailed", "message": "At least one resource deployment operation failed."},
    }
    base.update(changes)
    return base


def lines_of(file):
    return [json.loads(line) for line in file.read_text(encoding="utf-8").splitlines()] if file.exists() else []


class Calls:
    """What the fake Azure CLI was asked to do."""

    def read_calls(self, directory):
        self.calls = lines_of(directory / "calls.jsonl")
        # Per call, the SHA-256 of every NSS_*, ZSCALER_* and ZIA_* variable the call received.
        self.environments = lines_of(directory / "environment.jsonl")

    def environment_of(self, call):
        return self.environments[self.calls.index(call)]

    def calls_of(self, *command):
        return [call for call in self.calls if call[: len(command)] == list(command)]

    def started(self):
        return self.calls_of("deployment", "sub", "create")

    def previewed(self):
        return self.calls_of("deployment", "sub", "what-if")

    def values_after_parameters(self, call):
        """The name=value arguments of a what-if or create call, without the parameter file."""
        return [argument for argument in call if "=" in argument and not argument.startswith("-")]


class Result(Calls):
    def __init__(self, completed, directory):
        self.status = completed.returncode
        self.output = completed.stdout + completed.stderr
        self.read_calls(directory)


def command_for(
    kind,
    action,
    tmp_path,
    the_scenario,
    *,
    resource_group=GROUP,
    location="eastus",
    parameters="",
    yes=True,
    environment=None,
    through_make=False,
    poll_seconds="0",
):
    """The command line and the environment that run one script against the fake Azure CLI."""
    directory = tmp_path / "fake"
    directory.mkdir(exist_ok=True)
    (directory / "scenario.json").write_text(json.dumps(the_scenario), encoding="utf-8")
    az = directory / "az"
    az.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{REPOSITORY / "tests" / "fake_az.py"}" "$@"\n', encoding="utf-8"
    )
    az.chmod(0o755)

    env = {
        "PATH": f"{directory}{os.pathsep}{os.environ['PATH']}",
        "HOME": os.environ.get("HOME", str(tmp_path)),
        "TMPDIR": str(tmp_path),
        "FAKE_AZ_DIR": str(directory),
        # The answers of the questions never go to the file in the repository.
        "ANSWERS_FILE": str(tmp_path / "answers"),
        # No test asks a service on the internet for the address of this computer.
        "PUBLIC_IP_URL": "none",
        "NSS_SAS_TOKEN": SAS_TOKEN,
        "NSS_VM_PASSWORD": VM_PASSWORD,
        "ZSCALER_CLIENT_ID": NOT_A_CLIENT_ID,
        "ZSCALER_CLIENT_SECRET": CLIENT_SECRET,
        "ZSCALER_VANITY_DOMAIN": "example",
    }
    for name, value in (environment or {}).items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value

    if kind == "bash":
        env.update(RESOURCE_GROUP=resource_group, LOCATION=location, PARAMETERS=parameters, POLL_SECONDS=poll_seconds)
        if yes:
            env["ASSUME_YES"] = "1"
        if through_make:
            command = [MAKE, "--no-print-directory", "-C", str(REPOSITORY), action]
        else:
            command = [BASH, str(REPOSITORY / "scripts" / "deploy.sh"), action]
    else:
        command = [PWSH, "-NoProfile", "-File", str(REPOSITORY / "scripts" / "deploy.ps1"), action.capitalize()]
        command += ["-PollSeconds", poll_seconds]
        if resource_group:
            command += ["-ResourceGroup", resource_group]
        if location:
            command += ["-Location", location]
        if parameters:
            command += ["-Parameters", parameters]
        if yes and action == "deploy":
            command += ["-Yes"]

    return command, env, directory


def run(kind, action, tmp_path, the_scenario, *, stdin="", **settings):
    command, env, directory = command_for(kind, action, tmp_path, the_scenario, **settings)
    completed = subprocess.run(command, env=env, input=stdin, capture_output=True, text=True, timeout=180)
    return Result(completed, directory)


class Terminal(Calls):
    """A script that runs in a pseudo-terminal, as it does when a person starts it."""

    def __init__(
        self, kind, action, tmp_path, the_scenario, *, columns=100, environment=None, poll_seconds="0", **settings
    ):
        variables = {"TERM": "xterm-256color", "LANG": "en_US.UTF-8", "LC_ALL": None, "NO_COLOR": None}
        variables.update(environment or {})
        tmp_path.mkdir(exist_ok=True)
        command, env, self.directory = command_for(
            kind, action, tmp_path, the_scenario, environment=variables, poll_seconds=poll_seconds, **settings
        )
        import fcntl  # not on Windows, where this module is skipped
        import pty
        import termios

        self._master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, columns, 0, 0))

        def take_the_terminal():
            # The pseudo-terminal becomes the terminal of the script, so that Ctrl+C typed
            # into it reaches the script the way it does on a real terminal.
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        self.process = subprocess.Popen(
            command,
            env=env,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
            start_new_session=True,
            preexec_fn=take_the_terminal,
        )
        os.close(slave)
        self.raw = ""
        self._answered = 0
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def read_until(self, text=None, timeout=120):
        """Reads the output until the text has appeared, or to the end without a text."""
        deadline = time.monotonic() + timeout
        while text is None or text not in self.raw:
            if not select.select([self._master], [], [], max(0.0, deadline - time.monotonic()))[0]:
                raise AssertionError(f"no more output after {timeout} seconds:\n{self.raw}")
            try:
                data = os.read(self._master, 65536)
            except OSError:  # Linux reports the end of the output of a pseudo-terminal as an error
                data = b""
            if not data:
                if text is None:
                    break
                raise AssertionError(f"{text!r} did not appear:\n{self.raw}")
            self._take(data)
        return self

    def _take(self, data):
        """Keeps output, and answers like a terminal when the script asks where the cursor is.

        PowerShell asks before it reads a line, and takes what is typed for the answer when none comes.
        """
        text = self._decoder.decode(data)
        self.raw += text
        for _ in range(text.count("\x1b[6n")):
            os.write(self._master, b"\x1b[1;1R")

    def send(self, text):
        """Types a line."""
        os.write(self._master, (text + "\n").encode())
        return self

    def press_ctrl_c(self):
        os.write(self._master, b"\x03")
        return self

    def shows_input(self):
        """Whether the terminal shows what is typed."""
        import termios

        return bool(termios.tcgetattr(self._master)[3] & termios.ECHO)

    def answer(self, question, text, timeout=60):
        """Waits for a question that follows the last one that was answered, and types the answer."""
        deadline = time.monotonic() + timeout
        while question not in self.raw[self._answered :]:
            ready = select.select([self._master], [], [], max(0.0, deadline - time.monotonic()))[0]
            try:
                data = os.read(self._master, 65536) if ready else b""
            except OSError:
                data = b""
            if not data:
                raise AssertionError(f"{question!r} was not asked:\n{self.raw[self._answered:]}")
            self._take(data)
        self._answered = self.raw.index(question, self._answered) + len(question)
        return self.watch(0.05).send(text)

    def watch(self, seconds):
        """Keeps reading for a while, as a terminal does while the person at it does nothing.

        A script that asks where the cursor is gets its answer in that time, before anything is typed.
        """
        deadline = time.monotonic() + seconds
        while True:
            if not select.select([self._master], [], [], max(0.0, deadline - time.monotonic()))[0]:
                return self
            try:
                data = os.read(self._master, 65536)
            except OSError:
                data = b""
            if not data:
                return self
            self._take(data)

    def finish(self):
        self.read_until()
        self.status = self.process.wait(timeout=60)
        os.close(self._master)
        self.read_calls(self.directory)
        return self

    @property
    def text(self):
        """Everything that was printed, without colours, cursor movements and window titles."""
        return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", self._without_titles()).replace("\r\n", "\n")

    def _without_titles(self):
        """The output without the window titles PowerShell sets around every command it starts."""
        return re.sub(r"\x1b\][^\x07\x1b]*(\x07|\x1b\\)", "", self.raw)

    @property
    def colours(self):
        """The colour sequences in the output."""
        return re.findall(r"\x1b\[[0-9;]*m", self.raw)

    @property
    def screen(self):
        """The lines that are left on the screen after everything was drawn, without colours."""
        text = re.sub(r"\x1b\[[0-9;]*m", "", self._without_titles())
        lines, line, column, position = [], [], 0, 0
        while position < len(text):
            character = text[position]
            sequence = re.match(r"\x1b\[[0-9;?]*[A-Za-z]", text[position:])
            if sequence:
                if sequence.group(0) == "\x1b[K":
                    del line[column:]
                position += len(sequence.group(0))
                continue
            if character == "\r":
                column = 0
            elif character == "\n":
                lines.append("".join(line).rstrip())
                line, column = [], 0
            else:
                line[column : column + 1] = [character]
                column += 1
            position += 1
        if line:
            lines.append("".join(line).rstrip())
        return lines

    def frames(self, text):
        """Every line that was drawn and contains the text, also those that were drawn over."""
        plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", self._without_titles())
        return [piece for piece in re.split(r"[\r\n]", plain) if text in piece]


@pytest.mark.parametrize("kind", KINDS)
def test_a_resource_group_that_does_not_exist_is_created(kind, tmp_path):
    result = run(kind, "deploy", tmp_path, scenario())
    assert result.status == 0, result.output
    (call,) = result.started()
    assert result.values_after_parameters(call) == ["existingResourceGroupName=", f"newResourceGroupName={GROUP}"]
    assert call[call.index("--location") + 1] == "eastus"
    assert call[call.index("--name") + 1] == f"nss-{GROUP}"
    assert call[call.index("--parameters") + 1].endswith("main.sub.bicepparam")
    assert "--no-wait" in call
    assert f"{GROUP} (does not exist, will be created)" in result.output


@pytest.mark.parametrize("kind", KINDS)
def test_a_resource_group_that_exists_is_used_and_gives_the_location(kind, tmp_path):
    result = run(kind, "deploy", tmp_path, scenario(), resource_group="Existing-RG", location="")
    assert result.status == 0, result.output
    (call,) = result.started()
    assert result.values_after_parameters(call) == ["existingResourceGroupName=Existing-RG", "newResourceGroupName="]
    assert call[call.index("--location") + 1] == "westeurope"
    assert "Existing-RG (exists, will be used as it is)" in result.output


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("action", ["preview", "deploy"])
def test_a_resource_group_that_is_being_deleted_is_refused(kind, action, tmp_path):
    result = run(kind, action, tmp_path, scenario(deleting_groups=["Existing-RG"]), resource_group="Existing-RG")
    assert result.status == 1
    assert "is being deleted" in result.output and "az group exists --name Existing-RG" in result.output
    assert result.started() == []
    assert not any(call[:3] == ["deployment", "sub", "what-if"] for call in result.calls)


@pytest.mark.parametrize("kind", KINDS)
def test_a_new_resource_group_needs_a_location(kind, tmp_path):
    result = run(kind, "deploy", tmp_path, scenario(), location="")
    assert result.status == 1
    assert "does not exist" in result.output and "eastus" in result.output
    assert result.started() == []


@pytest.mark.parametrize("kind", KINDS)
def test_the_resource_group_is_required(kind, tmp_path):
    result = run(kind, "deploy", tmp_path, scenario(), resource_group="")
    assert result.status == 1
    assert "ResourceGroup" in result.output or "RESOURCE_GROUP" in result.output
    assert result.calls == []


@pytest.mark.parametrize("kind", KINDS)
def test_a_missing_sas_token_stops_before_azure_is_asked_anything(kind, tmp_path):
    result = run(kind, "deploy", tmp_path, scenario(), environment={"NSS_SAS_TOKEN": None})
    assert result.status == 1
    assert "NSS_SAS_TOKEN is not set" in result.output
    assert result.calls == []


@pytest.mark.parametrize("kind", KINDS)
def test_not_signed_in_is_reported(kind, tmp_path):
    result = run(kind, "deploy", tmp_path, scenario(account=None))
    assert result.status == 1
    assert "az login" in result.output
    assert result.started() == []


@pytest.mark.parametrize("kind", KINDS)
def test_progress_is_reported_step_by_step_and_nothing_twice(kind, tmp_path):
    result = run(kind, "deploy", tmp_path, scenario())
    assert result.status == 0, result.output
    expected = [
        f"] resource group {GROUP}: Succeeded",
        f"deployment into resource group {GROUP}: Running",
        "step networkSecurityGroup: Running",
        "Zscaler-NSS-VM-NSG (networkSecurityGroups): Running",
        "step networkSecurityGroup: Succeeded",
        "Zscaler-NSS-VM-NSG (networkSecurityGroups): Succeeded",
        "step vhdCopy: Running",
        "Zscaler-NSS-VM-vhd-copy (deploymentScripts): Running",
        "| Starting the copy.",
        "| VHD copy: 15% (76.3 of 500.0 GiB)",
        "| VHD copy: 97% (489.8 of 500.0 GiB)",
        "| VHD copy: 100% (500.0 of 500.0 GiB)",
        "| The VHD has been copied successfully.",
        "Zscaler-NSS-VM-vhd-copy (deploymentScripts): Succeeded",
        "step vhdCopy: Succeeded",
        "Zscaler-NSS-VM (virtualMachines): Succeeded",
        "step virtualMachine: Succeeded",
        f"deployment into resource group {GROUP}: Succeeded",
        "The deployment succeeded.",
    ]
    # A step that ended is reported after its content, and no empty log line is printed.
    assert [result.output.index(line) for line in expected[-7:]] == sorted(
        result.output.index(line) for line in expected[-7:]
    ), result.output
    assert not any(line.rstrip().endswith("|") for line in result.output.splitlines()), result.output
    for line in expected:
        assert result.output.count(line) == 1, f"{line!r} appears {result.output.count(line)} times\n{result.output}"
    in_order = [
        "step networkSecurityGroup: Running",
        "step networkSecurityGroup: Succeeded",
        "step vhdCopy: Running",
        "| Starting the copy.",
        "| VHD copy: 97% (489.8 of 500.0 GiB)",
        "| VHD copy: 100% (500.0 of 500.0 GiB)",
        "| The VHD has been copied successfully.",
        "step virtualMachine: Succeeded",
        "The deployment succeeded.",
    ]
    order = [result.output.index(line) for line in in_order]
    assert order == sorted(order), result.output
    # The hash Bicep appends to the name of a module deployment is not shown.
    assert "bbbbbbbbbbbbb" not in result.output
    # The copy is reported as a percentage, and output that is not a terminal gets no bar.
    assert "bytes copied" not in result.output
    assert "\r" not in result.output and "\x1b" not in result.output


@pytest.mark.parametrize("kind", KINDS)
def test_a_copy_line_that_is_not_progress_is_printed_as_it_is(kind, tmp_path):
    log = "VHD copy Pending: 0 of 0 bytes copied.\nVHD copy Failed: 7 of 10 bytes copied.\n"
    phases = [
        phase("Succeeded", "Succeeded", {"vhdCopy": "Succeeded"}, {"vhdCopy": [COPY_SCRIPT]}, {COPY_SCRIPT_NAME: log})
    ]
    result = run(kind, "watch", tmp_path, scenario(phases=phases))
    assert "| VHD copy Pending: 0 of 0 bytes copied." in result.output
    assert "| VHD copy Failed: 7 of 10 bytes copied." in result.output
    assert "%" not in result.output


CERTIFICATE_SCRIPT_NAME = "Zscaler-NSS-VM-nss-certificate"
CERTIFICATE_LOG = [
    "==> Connecting to zsroot@192.168.100.132:22.",
    "==> The nss command is /sc/bin/nss, found with the PATH of a login shell.",
    "==> Running: sudo nss install-cert NssCertificate.zip",
    "==> We trust you have received the usual lecture from the local System",
    "==> Administrator.",
    "==> ",
    "==> Certificate installation in progress",
    "==> The run failed.",
    "ERROR: 'sudo nss install-cert NssCertificate.zip' failed with exit status 3.",
]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("separator", [" ", "\n", "\r\n"], ids=["one line", "lines", "lines that end with CR LF"])
def test_the_certificate_log_is_cut_into_its_lines_again(kind, separator, tmp_path):
    """Azure returns the log of the Azure CLI deployment script with a space in place of every line end."""
    script = operation(SCRIPTS, CERTIFICATE_SCRIPT_NAME, "Running")
    phases = [
        phase(
            "Running",
            "Running",
            {"nssCertificate": "Running"},
            {"nssCertificate": [script]},
            {CERTIFICATE_SCRIPT_NAME: separator.join(CERTIFICATE_LOG[:count]) + separator},
        )
        for count in (2, 2, 3, len(CERTIFICATE_LOG))
    ]
    phases.append(
        phase(
            "Succeeded",
            "Succeeded",
            {"nssCertificate": "Succeeded"},
            {"nssCertificate": [operation(SCRIPTS, CERTIFICATE_SCRIPT_NAME, "Succeeded")]},
            {CERTIFICATE_SCRIPT_NAME: separator.join(CERTIFICATE_LOG) + separator},
        )
    )
    result = run(kind, "watch", tmp_path, scenario(phases=phases))

    shown = [line.split("| ", 1)[1] if "| " in line else "" for line in result.output.splitlines() if "|" in line]
    expected = [
        "Connecting to zsroot@192.168.100.132:22.",
        "The nss command is /sc/bin/nss, found with the PATH of a login shell.",
        "Running: sudo nss install-cert NssCertificate.zip",
        "We trust you have received the usual lecture from the local System",
        "Administrator.",
        "",
        "Certificate installation in progress",
    ]
    if separator == " ":
        # The error is written to the other output and has no mark, so it stays on the line before it.
        expected.append("The run failed. ERROR: 'sudo nss install-cert NssCertificate.zip' failed with exit status 3.")
    else:
        expected += ["The run failed.", "ERROR: 'sudo nss install-cert NssCertificate.zip' failed with exit status 3."]
    assert shown == expected, result.output
    assert "==>" not in result.output


def test_the_certificate_script_writes_the_mark_the_deploy_scripts_cut_the_log_at():
    script = (REPOSITORY / "scripts" / "install-nss-certificate.py").read_text(encoding="utf-8")
    assert 'LOG_MARK = "==> "' in script
    assert 'sub(/^[[:space:]]*==> /, ""); gsub(/[[:space:]]*==> /, "\\n")' in (
        REPOSITORY / "scripts" / "deploy.sh"
    ).read_text(encoding="utf-8")
    assert "-replace '^\\s*==> ', '') -split '\\s*==> '" in (REPOSITORY / "scripts" / "deploy.ps1").read_text(
        encoding="utf-8"
    )


def test_the_copy_script_writes_the_progress_line_the_deploy_scripts_read():
    copy_script = (REPOSITORY / "scripts" / "copy-vhd.ps1").read_text(encoding="utf-8")
    assert "'VHD copy {0}: {1:N0} of {2:N0} bytes copied.' -f $copyState.Status" in copy_script
    pattern = r"'^VHD copy (Pending|Success): ([0-9][0-9,.]*) of ([0-9][0-9,.]*) bytes copied\.$'"
    for name in ("deploy.sh", "deploy.ps1"):
        assert pattern in (REPOSITORY / "scripts" / name).read_text(encoding="utf-8"), name


BAR = r"\[\d\d:\d\d:\d\d\]       \| VHD copy (.) (\S+)  ( *\d+)%  ([\d.]+ of [\d.]+ GiB)"


@pytest.mark.parametrize("kind", KINDS)
def test_in_a_terminal_the_copy_is_one_bar_that_is_redrawn_in_place(kind, tmp_path):
    terminal = Terminal(kind, "deploy", tmp_path, scenario(), poll_seconds="1").finish()
    assert terminal.status == 0, terminal.raw
    screen = terminal.screen

    # What stays on the screen is one line for the whole copy, between the lines around it.
    bars = [line for line in screen if "VHD copy" in line]
    assert len(bars) == 1, screen
    assert re.fullmatch(BAR, bars[0]).groups() == ("✓", "█" * 30, "100", "500.0 of 500.0 GiB"), bars
    position = screen.index(bars[0])
    assert screen[position - 1].endswith("      | Starting the copy.")
    assert screen[position + 1].endswith("      | The VHD has been copied successfully.")
    assert screen[position + 2].endswith("    Zscaler-NSS-VM-vhd-copy (deploymentScripts): Succeeded")
    assert not any("█" in line or "░" in line for line in screen if line != bars[0]), screen
    assert "bytes copied" not in terminal.raw
    outputs = next(index for index, line in enumerate(screen) if line.startswith("resourceGroupName:"))
    assert screen[outputs - 2].endswith("] The deployment succeeded.") and screen[outputs - 1] == ""

    # Before that it showed each progress, with a spinner that turns.
    drawn = [re.search(BAR, frame).groups() for frame in terminal.frames("VHD copy")]
    assert {(cells, percent, text) for _, cells, percent, text in drawn} == {
        ("█" * 4 + "░" * 26, " 15", "76.3 of 500.0 GiB"),
        ("█" * 29 + "░", " 97", "489.8 of 500.0 GiB"),
        ("█" * 30, "100", "500.0 of 500.0 GiB"),
    }
    assert len({spinner for spinner, _, percent, _ in drawn if percent == " 15"}) >= 3, drawn


@pytest.mark.parametrize("kind", KINDS)
def test_a_line_that_is_printed_during_the_copy_goes_above_the_bar(kind, tmp_path):
    def during_the_copy(storage_state):
        return phase(
            "Running",
            "Running",
            {"vhdCopy": "Running", "storageAccount": storage_state},
            {
                "vhdCopy": [operation(SCRIPTS, COPY_SCRIPT_NAME, "Running")],
                "storageAccount": [operation("Microsoft.Storage/storageAccounts", "vhdstorageabc", storage_state)],
            },
            {COPY_SCRIPT_NAME: COPY_LOG_1},
        )

    phases = [during_the_copy("Running"), during_the_copy("Succeeded"), SUCCESSFUL_PHASES[-1]]
    terminal = Terminal(kind, "deploy", tmp_path, scenario(phases=phases), poll_seconds="1").finish()
    assert terminal.status == 0, terminal.raw
    lines = [line.split("] ", 1)[1] for line in terminal.screen if line.startswith("[")]
    start = lines.index("      | Starting the copy.")
    assert lines[start + 1 : start + 5] == [
        "  step storageAccount: Running",
        "    vhdstorageabc (storageAccounts): Running",
        "    vhdstorageabc (storageAccounts): Succeeded",
        "  step storageAccount: Succeeded",
    ], terminal.screen
    (bar,) = [line for line in lines[start + 5 :] if "VHD copy" in line]
    assert re.fullmatch(BAR, "[00:00:00] " + bar).group(3) == "100", terminal.screen
    assert not any("VHD copy" in line for line in lines[: start + 5])
    # The bar was on the screen while those lines were printed.
    assert any(" 15%" in frame for frame in terminal.frames("VHD copy"))


@pytest.mark.parametrize("kind", KINDS)
def test_the_bar_has_colours_unless_no_color_is_set(kind, tmp_path):
    coloured = Terminal(kind, "deploy", tmp_path / "coloured", scenario()).finish()
    assert "\x1b[38;5;46m█" in coloured.raw
    plain = Terminal(kind, "deploy", tmp_path / "plain", scenario(), environment={"NO_COLOR": "1"}).finish()
    assert plain.colours == []
    bars = [[line.split("] ")[1] for line in terminal.screen if "VHD copy" in line] for terminal in (plain, coloured)]
    assert bars[0] == bars[1] and len(bars[0]) == 1 and "█" * 30 in bars[0][0]


@pytest.mark.parametrize("kind", KINDS)
def test_the_bar_fits_the_terminal(kind, tmp_path):
    for columns, cells in ((80, 23), (72, 15)):
        terminal = Terminal(kind, "deploy", tmp_path / str(columns), scenario(), columns=columns).finish()
        frames = terminal.frames("VHD copy")
        assert max(len(frame) for frame in frames) == columns - 1, frames
        assert re.search(BAR, frames[-1]).group(2) == "█" * cells

    # Too narrow for a bar: one line for each progress, as without a terminal.
    narrow = Terminal(kind, "deploy", tmp_path / "narrow", scenario(), columns=60).finish()
    assert [line.split("] ")[1] for line in narrow.screen if "VHD copy" in line] == [
        "      | VHD copy: 15% (76.3 of 500.0 GiB)",
        "      | VHD copy: 97% (489.8 of 500.0 GiB)",
        "      | VHD copy: 100% (500.0 of 500.0 GiB)",
    ]


@pytest.mark.parametrize("kind", KINDS)
def test_without_utf8_the_bar_is_drawn_with_ascii(kind, tmp_path):
    terminal = Terminal(kind, "deploy", tmp_path, scenario(), environment={"LANG": "C"}).finish()
    frames = terminal.frames("VHD copy")
    assert re.search(BAR, frames[-1]).groups()[:3] == ("*", "#" * 30, "100"), frames
    assert any("####" + "-" * 26 in frame for frame in frames)
    assert all(ord(character) < 128 for frame in frames for character in frame)


@pytest.mark.parametrize("kind", KINDS)
def test_a_terminal_that_cannot_redraw_gets_lines(kind, tmp_path):
    terminal = Terminal(kind, "deploy", tmp_path, scenario(), environment={"TERM": "dumb"}).finish()
    assert sum("| VHD copy: " in line for line in terminal.screen) == 3, terminal.screen
    assert "█" not in terminal.raw


@pytest.mark.skipif(not BASH, reason="bash is not installed")
def test_ctrl_c_leaves_the_last_progress_and_ends_the_loop_that_draws_the_bar(tmp_path):
    # The copy never gets further than the third answer of the fake.
    terminal = Terminal("bash", "deploy", tmp_path, scenario(phases=SUCCESSFUL_PHASES[:3]), poll_seconds="2")
    terminal.read_until(" 97%")
    time.sleep(0.5)
    group = terminal.process.pid
    os.killpg(group, signal.SIGINT)
    terminal.finish()
    assert terminal.status == 130
    screen = terminal.screen
    bar = [line for line in screen if "VHD copy" in line]
    assert len(bar) == 1 and re.fullmatch(BAR, bar[0]).group(3) == " 97", screen
    assert screen[screen.index(bar[0]) + 2] == "Stopped watching. The deployment 'nss-NSS-RG' continues in Azure."
    # Nothing of the script is left behind, in particular not the loop that redraws the bar.
    time.sleep(0.5)
    with pytest.raises(ProcessLookupError):
        os.killpg(group, 0)


@pytest.mark.parametrize("kind", KINDS)
def test_the_outputs_are_printed_after_a_successful_deployment(kind, tmp_path):
    result = run(kind, "deploy", tmp_path, scenario())
    assert result.status == 0, result.output
    for name, value in OUTPUTS.items():
        line = next((line for line in result.output.splitlines() if line.strip().startswith(f"{name}:")), "")
        assert str(value["value"]).lower() in line.lower(), result.output
    assert "does not activate" in result.output
    assert NSS_NOT_RUNNING not in result.output


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("action", ["deploy", "outputs"])
def test_nss_that_did_not_start_is_pointed_out_below_the_outputs(kind, action, tmp_path):
    outputs = {**OUTPUTS, "nssRunning": {"type": "Bool", "value": False}}
    phases = SUCCESSFUL_PHASES[-1:] if action == "outputs" else SUCCESSFUL_PHASES
    result = run(kind, action, tmp_path, scenario(outputs=outputs, phases=phases))
    assert result.status == 0, result.output
    lines = [line.strip() for line in result.output.splitlines()]
    value_line = "nssRunning: False" if kind == "pwsh" else "nssRunning: false"
    assert lines.index(NSS_NOT_RUNNING + " See 'Start NSS' under") > lines.index(value_line)


@pytest.mark.parametrize("kind", KINDS)
def test_a_failed_deployment_ends_with_status_1_and_says_what_failed(kind, tmp_path):
    result = run(kind, "deploy", tmp_path, scenario(phases=FAILED_PHASES))
    assert result.status == 1, result.output
    assert "step nssCertificate: Failed" in result.output
    assert "Zscaler-NSS-VM-nss-certificate (deploymentScripts): Failed" in result.output
    assert "| No answer after 15 minutes." in result.output
    assert "The deployment ended with the state Failed." in result.output
    assert "Zscaler-NSS-VM-nss-certificate: DeploymentScriptError" in result.output
    assert (
        "    The provided script failed with the following error:\n    ERROR: The NSS VM did not answer"
        in result.output
    )
    assert "The deployment succeeded." not in result.output


@pytest.mark.parametrize("kind", KINDS)
def test_a_deployment_azure_refuses_is_not_watched(kind, tmp_path):
    result = run(kind, "deploy", tmp_path, scenario(create_error="InvalidTemplate: the template is not valid."))
    assert result.status == 1
    assert "InvalidTemplate" in result.output
    assert "The deployment was not started." in result.output
    assert result.calls_of("deployment", "sub", "show") == []


@pytest.mark.parametrize("kind", KINDS)
def test_preview_runs_what_if_and_never_deploys(kind, tmp_path):
    result = run(kind, "preview", tmp_path, scenario())
    assert result.status == 0, result.output
    (call,) = result.calls_of("deployment", "sub", "what-if")
    assert result.values_after_parameters(call) == ["existingResourceGroupName=", f"newResourceGroupName={GROUP}"]
    assert "Resource changes: 18 to create." in result.output
    assert result.started() == []


@pytest.mark.parametrize("kind", KINDS)
def test_replaced_values_follow_the_resource_group_and_only_their_names_are_shown(kind, tmp_path):
    parameters = "vhdRegion=znssprodeu sshSourceAddressPrefix=198.51.100.7/32"
    result = run(kind, "deploy", tmp_path, scenario(), parameters=parameters)
    assert result.status == 0, result.output
    (call,) = result.started()
    assert result.values_after_parameters(call)[2:] == parameters.split(" ")
    assert "vhdRegion sshSourceAddressPrefix" in result.output
    assert "198.51.100.7" not in result.output


@pytest.mark.parametrize("kind", KINDS)
def test_no_secret_is_printed_or_passed_as_an_argument(kind, tmp_path):
    for phases in (SUCCESSFUL_PHASES, FAILED_PHASES):
        directory = tmp_path / ("ok" if phases is SUCCESSFUL_PHASES else "failed")
        directory.mkdir()
        result = run(kind, "deploy", directory, scenario(phases=phases), parameters="installNssCertificate=true")
        for secret in SECRETS:
            assert secret not in result.output
            assert all(secret not in argument for call in result.calls for argument in call)
        assert "NSS_SAS_TOKEN" in result.output and "NSS_VM_PASSWORD" in result.output


@pytest.mark.parametrize("kind", KINDS)
def test_every_call_of_the_azure_cli_suppresses_warnings(kind, tmp_path):
    result = run(kind, "deploy", tmp_path, scenario())
    assert result.calls and all("--only-show-errors" in call for call in result.calls)


@pytest.mark.parametrize("kind", KINDS)
def test_the_certificate_step_without_the_vm_password_is_refused_before_azure_is_asked_anything(kind, tmp_path):
    missing = {"NSS_VM_PASSWORD": None}
    for action in ("deploy", "preview"):
        result = run(kind, action, tmp_path, scenario(), parameters="installNssCertificate=true", environment=missing)
        assert result.status == 1
        assert "installNssCertificate is true and NSS_VM_PASSWORD is not set" in result.output
        assert result.calls == []


@pytest.mark.parametrize("kind", KINDS)
def test_the_vm_password_is_not_asked_for_when_the_certificate_step_is_off(kind, tmp_path):
    result = run(kind, "deploy", tmp_path, scenario(), environment={"NSS_VM_PASSWORD": None})
    assert result.status == 0, result.output
    assert "WARNING" not in result.output


@pytest.mark.parametrize("kind", KINDS)
def test_the_certificate_step_without_complete_api_variables_gives_a_warning(kind, tmp_path):
    missing = {"ZSCALER_CLIENT_SECRET": None}
    result = run(kind, "preview", tmp_path, scenario(), parameters="installNssCertificate=true", environment=missing)
    assert result.status == 0, result.output
    assert "neither set of Zscaler API variables is complete" in result.output
    legacy = {
        "ZSCALER_CLIENT_ID": None,
        "ZIA_USERNAME": "u",
        "ZIA_PASSWORD": NOT_A_SECRET,
        "ZIA_API_KEY": NOT_A_SECRET,
        "ZIA_CLOUD": "c",
    }
    complete = run(kind, "preview", tmp_path, scenario(), parameters="installNssCertificate=true", environment=legacy)
    assert "WARNING" not in complete.output
    assert "WARNING" not in run(kind, "preview", tmp_path, scenario(), parameters="installNssCertificate=true").output


@pytest.mark.parametrize("kind", KINDS)
def test_watch_follows_a_deployment_that_is_already_running(kind, tmp_path):
    result = run(kind, "watch", tmp_path, scenario(), location="")
    assert result.status == 0, result.output
    assert result.started() == []
    assert all(
        call[call.index("--name") + 1] == f"nss-{GROUP}" for call in result.calls_of("deployment", "sub", "show")
    )
    assert "step vhdCopy: Succeeded" in result.output


@pytest.mark.parametrize("kind", KINDS)
def test_watch_gives_up_on_a_deployment_that_does_not_exist(kind, tmp_path):
    result = run(kind, "watch", tmp_path, scenario(deployment_missing=True), location="")
    assert result.status == 1
    assert f"The deployment 'nss-{GROUP}' cannot be read" in result.output


@pytest.mark.parametrize("kind", KINDS)
def test_outputs_prints_the_outputs_of_the_last_deployment(kind, tmp_path):
    result = run(kind, "outputs", tmp_path, scenario(phases=SUCCESSFUL_PHASES[-1:]), location="")
    assert result.status == 0, result.output
    assert "203.0.113.20" in result.output and "4242" in result.output
    assert result.started() == []


@pytest.mark.parametrize("kind", KINDS)
def test_a_parameter_file_for_main_bicep_is_refused(kind, tmp_path):
    environment = {"PARAMETER_FILE": str(REPOSITORY / "main.bicepparam")}
    result = run(kind, "deploy", tmp_path, scenario(), environment=environment)
    assert result.status == 1
    assert "is not a parameter file for main.sub.bicep" in result.output
    assert result.calls == []


@pytest.mark.skipif(not BASH, reason="bash is not installed")
def test_bash_does_not_deploy_without_a_terminal_to_ask_in(tmp_path):
    result = run("bash", "deploy", tmp_path, scenario(), yes=False, stdin="y\n")
    assert result.status == 1
    assert "ASSUME_YES=1" in result.output
    assert result.started() == []


@pytest.mark.skipif(not PWSH, reason="PowerShell 7 (pwsh) is not installed")
@pytest.mark.parametrize("answer, deployed", [("n\n", False), ("\n", False), ("y\n", True)])
def test_powershell_deploys_only_after_the_answer_yes(tmp_path, answer, deployed):
    result = run("pwsh", "deploy", tmp_path, scenario(), yes=False, stdin=answer)
    assert result.status == 0, result.output
    assert bool(result.started()) is deployed
    assert ("Nothing was deployed." in result.output) is not deployed


@pytest.mark.skipif(not (BASH and MAKE), reason="make or bash is not installed")
@pytest.mark.parametrize("target", ["preview", "deploy", "watch", "outputs"])
def test_the_makefile_passes_its_variables_to_the_script(tmp_path, target):
    phases = SUCCESSFUL_PHASES[-1:] if target == "outputs" else SUCCESSFUL_PHASES
    result = run("bash", target, tmp_path, scenario(phases=phases), through_make=True)
    assert result.status == 0, result.output
    if target == "deploy":
        (call,) = result.started()
        assert f"newResourceGroupName={GROUP}" in call
    if target == "outputs":
        assert "203.0.113.20" in result.output


@pytest.mark.skipif(not MAKE, reason="make is not installed")
def test_make_without_a_target_prints_the_help_and_runs_nothing(tmp_path):
    completed = subprocess.run(
        [MAKE, "--no-print-directory", "-C", str(REPOSITORY)], capture_output=True, text=True, timeout=60
    )
    assert completed.returncode == 0
    assert "make deploy" in completed.stdout and "RESOURCE_GROUP" in completed.stdout


def test_both_scripts_know_the_same_actions_and_secret_variables():
    shell = (REPOSITORY / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    powershell = (REPOSITORY / "scripts" / "deploy.ps1").read_text(encoding="utf-8")
    line = next(line for line in shell.splitlines() if line.startswith("SECRET_VARIABLES="))
    variables = line.split('"')[1].split(" ")
    assert len(variables) == 11
    for variable in variables:
        assert f"'{variable}'" in powershell
    for action in ("preview", "deploy", "watch", "outputs", "destroy"):
        assert f"  {action})" in shell
        assert f"'{action.capitalize()}'" in powershell


# The questions. Without a resource group, preview and deploy ask for the values.

LOCATIONS = ["eastus", "canadacentral", "westeurope", "eastusstage"]
NO_SECRETS = dict.fromkeys(
    ["NSS_SAS_TOKEN", "NSS_VM_PASSWORD", "ZSCALER_CLIENT_ID", "ZSCALER_CLIENT_SECRET", "ZSCALER_VANITY_DOMAIN"]
)
ASKED_HERE = "so the values of the deployment are asked for here"
USE_AGAIN = "Use these answers again?"
NEW_GROUP = "Resource group, used as it is"
REGION = "Azure region"
VM_NAME = "Name of the NSS VM"
SSH = "may reach the VM on SSH"
NETWORK_DEFAULTS = "Use the names and address ranges"
CERTIFICATE = "Register the VM in ZIA"
CLOUD = "Zscaler cloud, Enter for the production cloud"
SAS_QUESTION = "SAS token of the NSS image"

# The values a deployment gets from the answers of walk().
WALK_VALUES = [
    "existingResourceGroupName=",
    "newResourceGroupName=My-RG",
    "vmName=Zscaler-NSS-VM",
    "virtualMachineSize=Standard_D4_v4",
    "vhdRegion=znssprod",
    "sshSourceAddressPrefix=203.0.113.7",
    "vNetNewOrExisting=new",
    "stgNewOrExisting=new",
    "installNssCertificate=false",
]
# And what is saved of them.
WALK_SAVED = [
    "RESOURCE_GROUP=My-RG",
    "LOCATION=canadacentral",
    "vmName=Zscaler-NSS-VM",
    "virtualMachineSize=Standard_D4_v4",
    "vhdRegion=znssprod",
    "sshSourceAddressPrefix=203.0.113.7",
    "vNetNewOrExisting=new",
    "NETWORK_DEFAULTS=true",
    "stgNewOrExisting=new",
    "installNssCertificate=false",
]


def walk(**changes):
    """The questions of the shortest deployment, in their order, and an answer to each.

    A change gives a question another answer. A list of (question, answer) takes the place of
    the question, and None leaves it out because it is not asked.
    """
    steps = {
        "group": (NEW_GROUP, "My-RG"),
        "region": (REGION, "canadacentral"),
        "vm": (VM_NAME, ""),
        "size": ("Choose 1-4", "2"),
        "image": ("Choose 1-7", ""),
        "ssh": (SSH, "203.0.113.7"),
        "network": ("Choose 1-2", ""),
        "defaults": (NETWORK_DEFAULTS, ""),
        "storage": ("Choose 1-2", ""),
        "certificate": (CERTIFICATE, ""),
    }
    answers = []
    for label, (question, text) in steps.items():
        change = changes.pop(label, text)
        if isinstance(change, list):
            answers += change
        elif change is not None:
            answers.append((question, change))
    assert not changes, f"not a question: {sorted(changes)}"
    return answers


def run_directory(tmp_path):
    """A directory of its own for every run, so that the calls of two runs are not mixed."""
    directory = tmp_path / f"run{len(list(tmp_path.glob('run*')))}"
    directory.mkdir()
    return directory


def answers_file(tmp_path):
    return tmp_path / "answers"


def saved_lines(tmp_path):
    lines = answers_file(tmp_path).read_text(encoding="utf-8").splitlines()
    return [line for line in lines if not line.startswith("#")]


def save(tmp_path, lines):
    answers_file(tmp_path).write_text(
        "# written by a test\n" + "".join(f"{line}\n" for line in lines), encoding="utf-8"
    )


def question_settings(tmp_path, settings):
    settings.setdefault("resource_group", "")
    settings.setdefault("location", "")
    environment = {"ANSWERS_FILE": str(answers_file(tmp_path))}
    environment.update(settings.get("environment") or {})
    settings["environment"] = environment
    settings.setdefault("the_scenario", scenario(locations=LOCATIONS))
    return settings.pop("the_scenario"), settings


def asked(kind, action, tmp_path, answers, **settings):
    """Starts a script in a terminal without a resource group and types the answers to its questions."""
    the_scenario, settings = question_settings(tmp_path, settings)
    settings.setdefault("yes", False)
    terminal = Terminal(kind, action, run_directory(tmp_path), the_scenario, **settings)
    for question, text in answers:
        terminal.answer(question, text)
    return terminal.finish()


def unasked(kind, action, tmp_path, **settings):
    """Starts a script without a resource group where nothing can be asked."""
    the_scenario, settings = question_settings(tmp_path, settings)
    return run(kind, action, run_directory(tmp_path), the_scenario, **settings)


def sha256(text):
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.mark.parametrize("kind", KINDS)
def test_without_a_resource_group_the_values_are_asked_for_in_a_terminal(kind, tmp_path):
    terminal = asked(kind, "deploy", tmp_path, walk() + [("Deploy now?", "y")])
    assert terminal.status == 0, terminal.text
    assert ASKED_HERE in terminal.text
    assert "My-RG does not exist and will be created." in terminal.text
    (call,) = terminal.started()
    assert call[call.index("--location") + 1] == "canadacentral"
    assert terminal.values_after_parameters(call) == WALK_VALUES
    for value in WALK_VALUES[2:]:
        assert value in terminal.text  # the summary above the confirmation shows what was answered
    assert "The deployment succeeded." in terminal.text
    assert saved_lines(tmp_path) == WALK_SAVED


@pytest.mark.parametrize("kind", KINDS)
def test_the_answers_are_saved_before_the_confirmation_and_nothing_is_deployed_without_it(kind, tmp_path):
    terminal = asked(kind, "deploy", tmp_path, walk() + [("Deploy now?", "n")])
    assert terminal.status == 0, terminal.text
    assert "Nothing was deployed." in terminal.text
    assert terminal.started() == []
    assert saved_lines(tmp_path) == WALK_SAVED


@pytest.mark.parametrize("kind", KINDS)
def test_an_answer_that_cannot_be_used_is_asked_for_again(kind, tmp_path):
    answers = walk(
        group=[(NEW_GROUP, "not a name!"), (NEW_GROUP, "My-RG")],
        region=[(REGION, "Canada"), (REGION, "canada"), (REGION, "eastusstage"), (REGION, "canadacentral")],
        vm=[(VM_NAME, "-vm"), (VM_NAME, "My-VM")],
        size=[("Choose 1-4", "5"), ("Choose 1-4", "Standard_D8_v4")],
        ssh=[(SSH, "203.0.113.7/24"), (SSH, "300.1.1.1"), (SSH, "everyone"), (SSH, "203.0.113.0/24")],
        certificate=[(CERTIFICATE, "maybe"), (CERTIFICATE, "n")],
    )
    terminal = asked(kind, "preview", tmp_path, answers)
    assert terminal.status == 0, terminal.text
    said = [
        "A resource group name has 1 to 90 letters",
        "A region is written in lower-case letters and digits",
        "'canada' is not a region of this subscription. The regions are: eastus canadacentral westeurope",
        # A region that is not a place, such as a stage of another region, cannot hold a resource group.
        "'eastusstage' is not a region of this subscription.",
        "A VM name has 1 to 64 letters",
        "Enter a number from 1 to 4.",
        "203.0.113.7/24 does not start at the first address of a range. The range it is in is 203.0.113.0/24.",
        "This is not an IPv4 address.",
        "An address range is written as an IPv4 address and a prefix length",
        "Answer yes or no.",
    ]
    position = 0
    for message in said:
        assert message in terminal.text[position:], f"{message!r} is missing or out of order:\n{terminal.text}"
        position = terminal.text.index(message, position)
    (call,) = terminal.previewed()
    assert terminal.values_after_parameters(call) == [
        "existingResourceGroupName=",
        "newResourceGroupName=My-RG",
        "vmName=My-VM",
        "virtualMachineSize=Standard_D8_v4",
        "vhdRegion=znssprod",
        "sshSourceAddressPrefix=203.0.113.0/24",
        "vNetNewOrExisting=new",
        "stgNewOrExisting=new",
        "installNssCertificate=false",
    ]
    assert terminal.started() == []


NETWORK = "Address range of the virtual network"
SERVICE = "Address range of the service subnet"
MANAGEMENT = "Address range of the management subnet"
CERTIFICATE_RANGE = "Address range of that subnet"


@pytest.mark.parametrize("kind", KINDS)
def test_the_ranges_of_a_new_network_are_asked_for_until_they_fit_together(kind, tmp_path):
    answers = walk(
        defaults=[
            (NETWORK_DEFAULTS, "n"),
            ("Name of the virtual network", "My-VNET"),
            ("Name of the service subnet", ""),
            ("Name of the management subnet", "mgmt"),
            (NETWORK, "10.20.0.0/24"),
            (SERVICE, "10.20.1.0/25"),
            (MANAGEMENT, "10.20.0.128/28"),
            # The service subnet is outside the network. Enter keeps the answer of the last round.
            (NETWORK, ""),
            (SERVICE, "10.20.0.0/25"),
            (MANAGEMENT, "10.20.0.64/26"),
            # The subnets overlap.
            (NETWORK, ""),
            (SERVICE, ""),
            (MANAGEMENT, "10.20.0.128/28"),
        ],
        certificate=[
            (CERTIFICATE, "y"),
            ("Name of the NSS server object", "My NSS"),
            ("Choose 1-11", "3"),
            ("Name of the subnet the certificate step runs in", ""),
            (CERTIFICATE_RANGE, "10.20.0.128/28"),
            (CERTIFICATE_RANGE, "10.20.0.144/28"),
            (CLOUD, ""),
        ],
    )
    terminal = asked(kind, "preview", tmp_path, answers)
    assert terminal.status == 0, terminal.text
    assert "The subnet 10.20.1.0/25 is not inside the virtual network 10.20.0.0/24." in terminal.text
    assert "The subnets 10.20.0.0/25 and 10.20.0.64/26 overlap." in terminal.text
    assert "The subnets 10.20.0.128/28 and 10.20.0.128/28 overlap." in terminal.text
    assert f"{SERVICE} [10.20.1.0/25]" in terminal.text
    (call,) = terminal.previewed()
    assert terminal.values_after_parameters(call) == [
        "existingResourceGroupName=",
        "newResourceGroupName=My-RG",
        "vmName=Zscaler-NSS-VM",
        "virtualMachineSize=Standard_D4_v4",
        "vhdRegion=znssprod",
        "sshSourceAddressPrefix=203.0.113.7",
        "vNetNewOrExisting=new",
        "virtualNetworkName=My-VNET",
        "subnetName=Zscaler-NSS-Subnet",
        "subnetName2=mgmt",
        "vNetAddressRange=10.20.0.0/24",
        "subnetAddressRange=10.20.0.0/25",
        "subnetAddressRange2=10.20.0.128/28",
        "stgNewOrExisting=new",
        "installNssCertificate=true",
        "nssServerName=My NSS",  # one argument, with its space
        "nssServerType=NSS_FOR_FIREWALL",
        "certificateSubnetName=Zscaler-NSS-Certificate-Subnet",
        "certificateSubnetAddressRange=10.20.0.144/28",
    ]


@pytest.mark.parametrize("kind", KINDS)
def test_an_existing_network_and_storage_account_are_asked_for_by_name(kind, tmp_path):
    answers = walk(
        region=None,
        ssh="*",
        network=[
            ("Choose 1-2", "2"),
            ("Name of the existing virtual network", "Their-VNET"),
            ("Resource group of that virtual network", "Net-RG"),
            ("subnet for the service interface", "svc"),
            ("subnet for the management interface", "mgmt"),
        ],
        defaults=None,
        storage=[
            ("Choose 1-2", "existing"),
            ("Name of the existing storage account", "Not_A_Name"),
            ("Name of the existing storage account", "mystorage01"),
        ],
        certificate=[
            (CERTIFICATE, "y"),
            ("Name of the NSS server object", ""),
            ("Choose 1-11", ""),
            ("Name of that subnet", "cert"),
            (CLOUD, ""),
        ],
    )
    the_scenario = scenario(locations=LOCATIONS, groups={"My-RG": "westeurope"})
    terminal = asked(kind, "preview", tmp_path, answers, the_scenario=the_scenario)
    assert terminal.status == 0, terminal.text
    assert "My-RG exists in westeurope and is used as it is." in terminal.text
    assert REGION not in terminal.text
    assert "A storage account name has 3 to 24 lower-case letters and digits." in terminal.text
    assert "The subnet must exist, be delegated to" in terminal.text
    (call,) = terminal.previewed()
    assert call[call.index("--location") + 1] == "westeurope"
    assert terminal.values_after_parameters(call) == [
        "existingResourceGroupName=My-RG",
        "newResourceGroupName=",
        "vmName=Zscaler-NSS-VM",
        "virtualMachineSize=Standard_D4_v4",
        "vhdRegion=znssprod",
        "sshSourceAddressPrefix=*",
        "vNetNewOrExisting=existing",
        "virtualNetworkName=Their-VNET",
        "virtualNetworkResourceGroup=Net-RG",
        "subnetName=svc",
        "subnetName2=mgmt",
        "stgNewOrExisting=existing",
        "nameStorageAccount=mystorage01",
        "installNssCertificate=true",
        "nssServerName=Zscaler-NSS-VM",
        "nssServerType=NSS_FOR_WEB",
        "certificateSubnetName=cert",
    ]


TYPED_SAS = NOT_A_TYPED_SAS
TYPED = {
    "oneapi": {
        "choice": "",
        "values": [("Zidentity vanity domain", "ZSCALER_VANITY_DOMAIN", "acme")],
        "secrets": [
            (SAS_QUESTION, "NSS_SAS_TOKEN", TYPED_SAS),
            ("Password of the zsroot account", "NSS_VM_PASSWORD", NOT_A_TYPED_VM_PASSWORD),
            ("OneAPI client ID", "ZSCALER_CLIENT_ID", NOT_A_TYPED_CLIENT_ID),
            ("OneAPI client secret", "ZSCALER_CLIENT_SECRET", NOT_A_TYPED_CLIENT_SECRET),
        ],
    },
    "legacy": {
        "choice": "2",
        "values": [
            ("Admin user name for the legacy ZIA API", "ZIA_USERNAME", "admin@example.com"),
            ("ZIA cloud name", "ZIA_CLOUD", "zscalertwo"),
        ],
        "secrets": [
            (SAS_QUESTION, "NSS_SAS_TOKEN", TYPED_SAS),
            ("Password of the zsroot account", "NSS_VM_PASSWORD", NOT_A_TYPED_VM_PASSWORD),
            ("Admin password for the legacy ZIA API", "ZIA_PASSWORD", NOT_A_TYPED_ZIA_PASSWORD),
            ("API key for the legacy ZIA API", "ZIA_API_KEY", NOT_A_TYPED_ZIA_API_KEY),
        ],
    },
}


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("api", ["oneapi", "legacy"])
def test_secrets_that_are_not_set_are_asked_for_without_showing_or_saving_them(kind, api, tmp_path):
    typed = TYPED[api]
    answers = walk(
        certificate=[(CERTIFICATE, "y"), ("Name of the NSS server object", ""), ("Choose 1-11", "")]
        + [("Choose 1-2", typed["choice"])]
        + [(question, value) for question, _, value in typed["values"]]
        + ([(CLOUD, "")] if api == "oneapi" else [])
    )
    answers += [(SAS_QUESTION, "a-text-that-is-not-a-token")]
    answers += [(question, value) for question, _, value in typed["secrets"]]
    terminal = asked(kind, "deploy", tmp_path, answers + [("Deploy now?", "y")], environment=NO_SECRETS)
    assert terminal.status == 0, terminal.text
    assert "This is not a SAS token: it has no sig= part." in terminal.text
    assert "(not shown, not saved)" in terminal.text

    secrets = [value for _, _, value in typed["secrets"]] + ["a-text-that-is-not-a-token"]
    saved = answers_file(tmp_path).read_text(encoding="utf-8")
    for secret in secrets:
        assert secret not in terminal.raw
        assert secret not in saved
        assert all(secret not in argument for call in terminal.calls for argument in call)

    # The Azure CLI, which compiles the parameter file, receives them in its environment.
    (call,) = terminal.started()
    received = terminal.environment_of(call)
    for _, name, value in typed["secrets"] + typed["values"]:
        assert received.get(name) == sha256(value), name
    # What is not a secret is saved, so the next run does not ask for it.
    assert f"ZSCALER_API={api}" in saved_lines(tmp_path)
    for _, name, value in typed["values"]:
        assert f"{name}={value}" in saved_lines(tmp_path)


@pytest.mark.parametrize("kind", KINDS)
def test_a_secret_that_is_set_is_not_asked_for(kind, tmp_path):
    answers = walk(
        certificate=[(CERTIFICATE, "y"), ("Name of the NSS server object", ""), ("Choose 1-11", ""), (CLOUD, "")]
    )
    terminal = asked(kind, "preview", tmp_path, answers)
    assert terminal.status == 0, terminal.text
    assert "(not shown, not saved)" not in terminal.text
    assert "Zscaler API credentials for the certificate step" not in terminal.text
    (call,) = terminal.previewed()
    assert terminal.environment_of(call)["NSS_SAS_TOKEN"] == sha256(SAS_TOKEN)


@pytest.mark.parametrize("kind", KINDS)
def test_a_value_given_with_the_command_is_not_asked_for(kind, tmp_path):
    given = "vmName=Given-VM virtualMachineSize=Standard_D8_v4 installNssCertificate=false"
    answers = walk(region=None, vm=None, size=None, certificate=None)
    terminal = asked(kind, "preview", tmp_path, answers, parameters=given, location="westeurope")
    assert terminal.status == 0, terminal.text
    for question in (REGION, VM_NAME, "Size of the VM", CERTIFICATE):
        assert question not in terminal.text
    (call,) = terminal.previewed()
    assert call[call.index("--location") + 1] == "westeurope"
    assert terminal.values_after_parameters(call) == [
        "existingResourceGroupName=",
        "newResourceGroupName=My-RG",
        "vhdRegion=znssprod",
        "sshSourceAddressPrefix=203.0.113.7",
        "vNetNewOrExisting=new",
        "stgNewOrExisting=new",
        "vmName=Given-VM",
        "virtualMachineSize=Standard_D8_v4",
        "installNssCertificate=false",
    ]
    # Only answers are saved.
    assert saved_lines(tmp_path) == [
        "RESOURCE_GROUP=My-RG",
        "vhdRegion=znssprod",
        "sshSourceAddressPrefix=203.0.113.7",
        "vNetNewOrExisting=new",
        "NETWORK_DEFAULTS=true",
        "stgNewOrExisting=new",
    ]


@pytest.mark.parametrize("kind", KINDS)
def test_the_answers_of_the_last_run_are_offered_again(kind, tmp_path):
    save(tmp_path, WALK_SAVED)
    before = answers_file(tmp_path).read_text(encoding="utf-8")

    terminal = asked(kind, "preview", tmp_path, [(USE_AGAIN, "")])
    assert terminal.status == 0, terminal.text
    assert "  vmName = Zscaler-NSS-VM" in terminal.text
    assert VM_NAME not in terminal.text
    (call,) = terminal.previewed()
    assert call[call.index("--location") + 1] == "canadacentral"
    assert terminal.values_after_parameters(call) == WALK_VALUES
    # Nothing was asked, so nothing is saved again.
    assert answers_file(tmp_path).read_text(encoding="utf-8") == before

    # With the answer no, every question is asked again and Enter keeps the saved answer.
    enter = [(question, "") for question, _ in walk()]
    terminal = asked(kind, "preview", tmp_path, [(USE_AGAIN, "n")] + enter)
    assert terminal.status == 0, terminal.text
    for default in ("[My-RG]", "[canadacentral]", "Choose 1-4 [2]", "[203.0.113.7]"):
        assert default in terminal.text
    (call,) = terminal.previewed()
    assert terminal.values_after_parameters(call) == WALK_VALUES
    assert saved_lines(tmp_path) == WALK_SAVED


@pytest.mark.skipif(not (BASH and PWSH), reason="bash or PowerShell 7 (pwsh) is not installed")
@pytest.mark.parametrize("first, second", [("bash", "pwsh"), ("pwsh", "bash")])
def test_the_answers_one_script_saved_are_read_by_the_other(first, second, tmp_path):
    terminal = asked(first, "preview", tmp_path, walk())
    assert terminal.status == 0, terminal.text
    # Plain lines without a byte order mark, whichever script wrote them.
    assert answers_file(tmp_path).read_bytes().startswith(b"# Answers to the questions")
    terminal = asked(second, "preview", tmp_path, [(USE_AGAIN, "")])
    assert terminal.status == 0, terminal.text
    (call,) = terminal.previewed()
    assert terminal.values_after_parameters(call) == WALK_VALUES
    result = unasked(second, "preview", tmp_path)
    assert result.status == 0, result.output
    (call,) = result.previewed()
    assert result.values_after_parameters(call) == WALK_VALUES


@pytest.mark.parametrize("kind", KINDS)
def test_a_value_that_may_be_empty_is_emptied_with_a_hyphen(kind, tmp_path):
    existing = [
        "RESOURCE_GROUP=My-RG",
        "vmName=Zscaler-NSS-VM",
        "virtualMachineSize=Standard_D2_v4",
        "vhdRegion=znssprod",
        "sshSourceAddressPrefix=*",
        "vNetNewOrExisting=existing",
        "virtualNetworkName=Their-VNET",
        "virtualNetworkResourceGroup=Net-RG",
        "subnetName=svc",
        "subnetName2=mgmt",
        "stgNewOrExisting=new",
        "installNssCertificate=false",
    ]
    save(tmp_path, existing)
    questions = [
        (NEW_GROUP, ""),
        (VM_NAME, ""),
        ("Choose 1-4", ""),
        ("Choose 1-7", ""),
        (SSH, ""),
        ("Choose 1-2", ""),
        ("Name of the existing virtual network", ""),
        ("Resource group of that virtual network", "-"),
        ("subnet for the service interface", ""),
        ("subnet for the management interface", ""),
        ("Choose 1-2", ""),
        (CERTIFICATE, ""),
    ]
    the_scenario = scenario(locations=LOCATIONS, groups={"My-RG": "westeurope"})
    terminal = asked(kind, "preview", tmp_path, [(USE_AGAIN, "n")] + questions, the_scenario=the_scenario)
    assert terminal.status == 0, terminal.text
    assert "[Net-RG, - for none]" in terminal.text
    (call,) = terminal.previewed()
    assert "virtualNetworkResourceGroup=" in call
    assert saved_lines(tmp_path) == [line.replace("=Net-RG", "=") for line in existing]


@pytest.mark.parametrize("kind", KINDS)
def test_a_saved_answer_that_cannot_be_used_is_asked_for_and_the_file_is_never_run(kind, tmp_path):
    marker = tmp_path / "the-answers-file-was-run"
    lines = [line for line in WALK_SAVED if not line.startswith(("vmName=", "virtualMachineSize="))]
    lines += [
        f"vmName=$(touch {marker})",
        "virtualMachineSize=Standard_A1",
        "notAnAnswer=value",
        "ASSUME_YES=1",
        "PARAMETERS=installNssCertificate=true",
        f"; touch {marker}",
    ]
    save(tmp_path, lines)
    # A name that is not an answer is not read: the confirmation is still asked, and the
    # certificate step stays off.
    answers = [(USE_AGAIN, ""), (VM_NAME, "My-VM"), ("Choose 1-4", ""), ("Deploy now?", "n")]
    terminal = asked(kind, "deploy", tmp_path, answers)
    assert terminal.status == 0, terminal.text
    assert "The saved value of vmName cannot be used. A VM name has 1 to 64 letters" in terminal.text
    assert not marker.exists()
    assert "installNssCertificate=false" in terminal.text
    assert terminal.started() == []
    assert saved_lines(tmp_path) == [
        line.replace("vmName=Zscaler-NSS-VM", "vmName=My-VM").replace("Standard_D4_v4", "Standard_D2_v4")
        for line in WALK_SAVED
    ]


@pytest.mark.parametrize("kind", KINDS)
def test_where_nothing_can_be_asked_the_saved_answers_are_used(kind, tmp_path):
    save(tmp_path, WALK_SAVED)
    result = unasked(kind, "deploy", tmp_path)
    assert result.status == 0, result.output
    assert "The values are taken from the answers in" in result.output
    assert ASKED_HERE not in result.output
    (call,) = result.started()
    assert result.values_after_parameters(call) == WALK_VALUES


UNFIT = [line for line in WALK_SAVED if not line.startswith("NETWORK_DEFAULTS=")] + [
    "NETWORK_DEFAULTS=false",
    "virtualNetworkName=My-VNET",
    "subnetName=svc",
    "subnetName2=mgmt",
    "vNetAddressRange=10.20.0.0/24",
    "subnetAddressRange=10.20.0.0/25",
    "subnetAddressRange2=10.20.0.0/26",
]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "lines, environment, message",
    [
        (
            [line for line in WALK_SAVED if not line.startswith("sshSourceAddressPrefix=")],
            {},
            "sshSourceAddressPrefix is not answered in",
        ),
        (UNFIT, {}, "The subnets 10.20.0.0/25 and 10.20.0.0/26 overlap. Correct the values in"),
        (WALK_SAVED, {"NSS_SAS_TOKEN": None}, "NSS_SAS_TOKEN is not set, and this is not a"),
    ],
    ids=["an answer is missing", "the ranges do not fit together", "a secret is not set"],
)
def test_where_nothing_can_be_asked_a_missing_value_ends_the_run(kind, lines, environment, message, tmp_path):
    save(tmp_path, lines)
    result = unasked(kind, "deploy", tmp_path, environment=environment)
    assert result.status == 1, result.output
    assert message in result.output
    assert result.started() == [] and result.previewed() == []


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("action", ["watch", "outputs"])
def test_watch_and_outputs_use_the_resource_group_that_was_answered(kind, action, tmp_path):
    save(tmp_path, WALK_SAVED)
    phases = SUCCESSFUL_PHASES[-1:] if action == "outputs" else SUCCESSFUL_PHASES
    result = unasked(kind, action, tmp_path, the_scenario=scenario(phases=phases))
    assert result.status == 0, result.output
    assert "Resource group 'My-RG', from the answers in" in result.output
    shown = result.calls_of("deployment", "sub", "show")
    assert shown and all(call[call.index("--name") + 1] == "nss-My-RG" for call in shown)
    assert "203.0.113.20" in result.output


@pytest.mark.parametrize("kind", KINDS)
def test_with_a_resource_group_nothing_is_asked_and_the_saved_answers_are_not_used(kind, tmp_path):
    save(tmp_path, WALK_SAVED)
    before = answers_file(tmp_path).read_text(encoding="utf-8")
    settings = {"environment": {"ANSWERS_FILE": str(answers_file(tmp_path))}}
    terminal = Terminal(kind, "preview", run_directory(tmp_path), scenario(), **settings).finish()
    assert terminal.status == 0, terminal.text
    assert ASKED_HERE not in terminal.text and USE_AGAIN not in terminal.text
    assert "Answered values" not in terminal.text
    (call,) = terminal.previewed()
    assert terminal.values_after_parameters(call) == ["existingResourceGroupName=", f"newResourceGroupName={GROUP}"]
    assert answers_file(tmp_path).read_text(encoding="utf-8") == before


@pytest.mark.parametrize("kind", KINDS)
def test_ctrl_c_at_a_question_deploys_nothing(kind, tmp_path):
    the_scenario, settings = question_settings(tmp_path, {})
    terminal = Terminal(kind, "deploy", run_directory(tmp_path), the_scenario, yes=False, **settings)
    terminal.read_until(NEW_GROUP).watch(0.5)
    terminal.press_ctrl_c().finish()
    assert "Stopped. Nothing was deployed." in terminal.text
    assert terminal.started() == [] and terminal.previewed() == []
    assert not answers_file(tmp_path).exists()
    if kind == "bash":
        assert terminal.status == 130


@pytest.mark.parametrize("kind", KINDS)
def test_ctrl_c_at_a_secret_deploys_nothing_and_leaves_the_terminal_as_it_was(kind, tmp_path):
    the_scenario, settings = question_settings(tmp_path, {"environment": {"NSS_SAS_TOKEN": None}})
    terminal = Terminal(kind, "deploy", run_directory(tmp_path), the_scenario, yes=False, **settings)
    for question, text in walk():
        terminal.answer(question, text)
    terminal.read_until(SAS_QUESTION).watch(0.5)
    assert not terminal.shows_input()
    terminal.press_ctrl_c().read_until()
    assert "Stopped. Nothing was deployed." in terminal.text
    assert terminal.shows_input()
    terminal.finish()
    assert terminal.started() == []
    # The answers were saved before the secrets were asked for.
    assert saved_lines(tmp_path) == WALK_SAVED


@pytest.mark.skipif(not (BASH and MAKE), reason="make or bash is not installed")
def test_make_without_a_resource_group_asks_too(tmp_path):
    terminal = asked("bash", "preview", tmp_path, walk(), through_make=True)
    assert terminal.status == 0, terminal.text
    (call,) = terminal.previewed()
    assert terminal.values_after_parameters(call) == WALK_VALUES
    assert saved_lines(tmp_path) == WALK_SAVED


def bicep_allowed(name):
    """The values main.bicep allows for a parameter, in its order."""
    text = (REPOSITORY / "main.bicep").read_text(encoding="utf-8")
    block = re.search(r"@allowed\(\[\n((?:\s+'[^']*'\n)+)\]\)\nparam " + name + r" ", text)
    return re.findall(r"'([^']*)'", block.group(1))


def test_both_scripts_ask_for_the_same_values_and_offer_what_the_template_allows():
    shell = (REPOSITORY / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    powershell = (REPOSITORY / "scripts" / "deploy.ps1").read_text(encoding="utf-8")

    names = re.search(r'^ANSWER_NAMES="([^"]*)"$', shell, re.M).group(1).split(" ")
    assert re.findall(r"'(\w+)'", re.search(r"\$answerNames = @\((.*?)\n\)", powershell, re.S).group(1)) == names
    # Every answer that is not a setting of the scripts is a parameter of the template.
    template = (REPOSITORY / "main.sub.bicep").read_text(encoding="utf-8")
    parameters = [name for name in names if name != name.upper()]
    assert len(parameters) == 19
    for name in parameters:
        assert re.search(rf"^param {name} ", template, re.M), name

    lists = {
        "virtualMachineSize": ("VM_SIZE_OPTIONS", "vmSizeOptions"),
        "vhdRegion": ("VHD_REGION_OPTIONS", "vhdRegionOptions"),
        "nssServerType": ("SERVER_TYPE_OPTIONS", "serverTypeOptions"),
    }
    for parameter, (shell_name, powershell_name) in lists.items():
        in_shell = re.findall(r'"([^"]*)"', re.search(rf"^{shell_name}=\((.*?)\n\)", shell, re.S | re.M).group(1))
        in_powershell = re.findall(
            r"'([^']*)'", re.search(rf"^\${powershell_name} = @\((.*?)\n\)", powershell, re.S | re.M).group(1)
        )
        assert in_shell == in_powershell
        assert [option.split("|")[0] for option in in_shell] == bicep_allowed(parameter)
    for parameter in ("vNetNewOrExisting", "stgNewOrExisting"):
        assert bicep_allowed(parameter) == ["new", "existing"]


# The question about the SSH rule offers the public IP address of the computer.

PUBLIC_IP = "Your public IP address is"


class AddressService:
    """A local stand-in for a service that tells a caller its public IP address."""

    def __init__(self):
        service = self
        self.answers = {}
        self.requests = []
        self.release = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                service.requests.append(self.path)
                status, body = service.answers.get(self.path, (404, "not found"))
                if status == "never":
                    service.release.wait(30)
                    status = 200
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *arguments):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, path, body="198.51.100.7", status=200):
        self.answers[path] = (status, body)
        return f"http://127.0.0.1:{self.server.server_address[1]}{path}"

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def address_service():
    service = AddressService()
    yield service
    service.close()


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "body, typed, allowed",
    [
        ("198.51.100.7", "", "198.51.100.7"),
        ("  198.51.100.7\r\n", "", "198.51.100.7"),
        ("198.51.100.7", "203.0.113.0/24", "203.0.113.0/24"),
        ("198.51.100.7", "*", "*"),
    ],
    ids=["Enter takes it", "with spaces and a line end", "a range is typed instead", "every address is typed instead"],
)
def test_the_public_ip_address_is_offered_for_the_ssh_rule(kind, body, typed, allowed, tmp_path, address_service):
    url = address_service.url("/ip", body)
    terminal = asked(kind, "preview", tmp_path, walk(ssh=typed), environment={"PUBLIC_IP_URL": url})
    assert terminal.status == 0, terminal.text
    assert f"{PUBLIC_IP} 198.51.100.7, as {url} sees it. Enter allows SSH from this address only." in terminal.text
    assert "(* is every address) [198.51.100.7]" in terminal.text
    (call,) = terminal.previewed()
    assert f"sshSourceAddressPrefix={allowed}" in call
    assert f"sshSourceAddressPrefix={allowed}" in saved_lines(tmp_path)
    assert address_service.requests == ["/ip"]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "body, status",
    [
        ("<html><body>198.51.100.7</body></html>", 200),
        ("198.51.100.7 and more", 200),
        ("198.51.100.7\n203.0.113.9", 200),
        ("999.51.100.7", 200),
        ("198.51.100", 200),
        ("198.51.100.7/24", 200),
        ("2001:db8::7", 200),
        ("$(touch MARKER)", 200),
        ("", 200),
        ("198.51.100.7", 500),
        ("198.51.100.7", 302),
    ],
    ids=[
        "a page",
        "an address and more",
        "two addresses",
        "a number above 255",
        "three numbers",
        "a range",
        "an IPv6 address",
        "a command",
        "nothing",
        "an error",
        "a redirection",
    ],
)
def test_an_answer_that_is_not_an_ip_address_is_not_offered(kind, body, status, tmp_path, address_service):
    marker = tmp_path / "the-answer-was-run"
    url = address_service.url("/ip", body.replace("MARKER", str(marker)), status)
    answers = walk(ssh=[(SSH, ""), (SSH, "203.0.113.7")])
    terminal = asked(kind, "preview", tmp_path, answers, environment={"PUBLIC_IP_URL": url})
    assert terminal.status == 0, terminal.text
    assert PUBLIC_IP not in terminal.text
    assert "(* is every address): " in terminal.text
    assert "A value is needed." in terminal.text
    (call,) = terminal.previewed()
    assert terminal.values_after_parameters(call) == WALK_VALUES
    assert not marker.exists()
    assert address_service.requests == ["/ip"]


@pytest.mark.parametrize("kind", KINDS)
def test_the_next_service_is_asked_when_one_does_not_answer(kind, tmp_path, address_service):
    closed = "http://127.0.0.1:1/ip"  # nothing listens on port 1
    urls = [closed, address_service.url("/broken", "no", 503), address_service.url("/second", "198.51.100.8")]
    terminal = asked(kind, "preview", tmp_path, walk(ssh=""), environment={"PUBLIC_IP_URL": " ".join(urls)})
    assert terminal.status == 0, terminal.text
    assert f"{PUBLIC_IP} 198.51.100.8, as {urls[2]} sees it." in terminal.text
    (call,) = terminal.previewed()
    assert "sshSourceAddressPrefix=198.51.100.8" in call
    assert address_service.requests == ["/broken", "/second"]


@pytest.mark.parametrize("kind", KINDS)
def test_a_service_that_does_not_answer_in_time_is_given_up_on(kind, tmp_path, address_service):
    url = address_service.url("/slow", "198.51.100.7", "never")
    started = time.monotonic()
    terminal = asked(kind, "preview", tmp_path, walk(), environment={"PUBLIC_IP_URL": url})
    assert terminal.status == 0, terminal.text
    assert time.monotonic() - started < 25
    assert PUBLIC_IP not in terminal.text
    (call,) = terminal.previewed()
    assert terminal.values_after_parameters(call) == WALK_VALUES


@pytest.mark.parametrize("kind", KINDS)
def test_without_a_service_the_ssh_rule_is_asked_for_as_before(kind, tmp_path):
    terminal = asked(kind, "preview", tmp_path, walk(), environment={"PUBLIC_IP_URL": "none"})
    assert terminal.status == 0, terminal.text
    assert PUBLIC_IP not in terminal.text
    assert "(* is every address): " in terminal.text


@pytest.mark.parametrize("kind", KINDS)
def test_no_service_is_asked_when_the_ssh_rule_is_not_asked_for(kind, tmp_path, address_service):
    environment = {"PUBLIC_IP_URL": address_service.url("/ip")}

    # A resource group is given, so nothing is asked.
    settings = {"environment": dict(environment, ANSWERS_FILE=str(answers_file(tmp_path)))}
    terminal = Terminal(kind, "preview", run_directory(tmp_path), scenario(), **settings).finish()
    assert terminal.status == 0, terminal.text

    # The value is given with the command.
    given = "sshSourceAddressPrefix=203.0.113.9"
    terminal = asked(kind, "preview", tmp_path, walk(ssh=None), parameters=given, environment=environment)
    assert terminal.status == 0, terminal.text
    (call,) = terminal.previewed()
    assert given in call

    # The answers of the last run are used again, in a terminal and without one.
    save(tmp_path, WALK_SAVED)
    terminal = asked(kind, "preview", tmp_path, [(USE_AGAIN, "")], environment=environment)
    assert terminal.status == 0, terminal.text
    result = unasked(kind, "preview", tmp_path, environment=environment)
    assert result.status == 0, result.output
    (call,) = result.previewed()
    assert "sshSourceAddressPrefix=203.0.113.7" in call

    assert PUBLIC_IP not in terminal.text + result.output
    assert address_service.requests == []


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "typed, allowed", [("", "203.0.113.7"), ("198.51.100.7", "198.51.100.7")], ids=["Enter", "the new address"]
)
def test_the_saved_ssh_rule_stays_the_default_when_the_address_has_changed(
    kind, typed, allowed, tmp_path, address_service
):
    save(tmp_path, WALK_SAVED)
    url = address_service.url("/ip")
    answers = [(USE_AGAIN, "n")] + [(question, typed if question == SSH else "") for question, _ in walk()]
    terminal = asked(kind, "preview", tmp_path, answers, environment={"PUBLIC_IP_URL": url})
    assert terminal.status == 0, terminal.text
    assert f"{PUBLIC_IP} 198.51.100.7, as {url} sees it. The answer of the last run is 203.0.113.7." in terminal.text
    assert "(* is every address) [203.0.113.7]" in terminal.text
    (call,) = terminal.previewed()
    assert f"sshSourceAddressPrefix={allowed}" in call


@pytest.mark.parametrize("kind", KINDS)
def test_a_saved_ssh_rule_that_cannot_be_used_is_replaced_by_the_address_that_is_offered(
    kind, tmp_path, address_service
):
    save(tmp_path, [line.replace("=203.0.113.7", "=somewhere") for line in WALK_SAVED])
    url = address_service.url("/ip")
    terminal = asked(kind, "preview", tmp_path, [(USE_AGAIN, ""), (SSH, "")], environment={"PUBLIC_IP_URL": url})
    assert terminal.status == 0, terminal.text
    assert "Enter allows SSH from this address only." in terminal.text
    assert "The saved value of sshSourceAddressPrefix cannot be used." in terminal.text
    (call,) = terminal.previewed()
    assert "sshSourceAddressPrefix=198.51.100.7" in call


@pytest.mark.skipif(not (BASH and MAKE), reason="make or bash is not installed")
def test_make_passes_the_address_service_on(tmp_path, address_service):
    url = address_service.url("/ip")
    terminal = asked("bash", "preview", tmp_path, walk(ssh=""), through_make=True, environment={"PUBLIC_IP_URL": url})
    assert terminal.status == 0, terminal.text
    (call,) = terminal.previewed()
    assert "sshSourceAddressPrefix=198.51.100.7" in call


def test_both_scripts_ask_the_same_services_by_default():
    shell = (REPOSITORY / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    powershell = (REPOSITORY / "scripts" / "deploy.ps1").read_text(encoding="utf-8")
    in_shell = re.search(r'^PUBLIC_IP_URL="\$\{PUBLIC_IP_URL:-([^}]*)\}"$', shell, re.M).group(1).split(" ")
    in_powershell = re.findall(
        r"'([^']*)'", re.search(r"^    \$PublicIpUrl = @\(('https.*)\)$", powershell, re.M).group(1)
    )
    assert in_shell == in_powershell
    assert len(in_shell) == 2 and all(url.startswith("https://") for url in in_shell)
