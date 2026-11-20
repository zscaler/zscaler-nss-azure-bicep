#!/usr/bin/env python3
"""Registers the NSS virtual machine in ZIA, installs its SSL certificate and configures it.

The script finds the NSS server object by name in ZIA, or creates it, downloads the
certificate ZIP file of that object, copies it to the NSS virtual machine over SSH and
installs it with `sudo nss install-cert`, as described in the Zscaler NSS deployment guide.

Before that, when the address and the gateway of the service interface are given, it answers
the questions of `sudo nss configure` with them. After the certificate, when asked to, it
downloads the NSS binaries with `sudo nss update-now`, starts NSS with `sudo nss start`, checks
with `sudo nss status` that it runs, and runs `sudo nss enable-autostart`.

It runs inside the deployment script of modules/nss-certificate.bicep and can also be run
from a workstation that can reach the virtual machine on SSH. All input comes from
environment variables, see read_settings(). It needs the paramiko package and
zscaler-sdk-python 1.11.5 or later. Every call to ZIA goes through that SDK.

It is safe to run again: an NSS server object that already exists is reused, a certificate
that is already installed on the virtual machine is not installed again, a service
interface that already has the given values is not configured again, and an NSS that already
runs is neither updated nor started again.

The script never activates the ZIA configuration. A new NSS server object stays a pending
change until the user activates it in ZIA.
"""

import base64
import hashlib
import io
import ipaddress
import json
import os
import re
import socket
import sys
import time
import zipfile

CERTIFICATE_FILE_NAME = "NssCertificate.zip"
MARKER_FILE_NAME = ".nss-certificate-installed"
CONFIGURE_MARKER_FILE_NAME = ".nss-service-interface-configured"
# The questions of `nss configure`, as the NSS 5.2 image for Azure asks them, each with the most
# times it may be asked in one run. A question is the last line of the output, without a line end.
# The command lists the name servers one by one before it asks whether to add one.
NAME_SERVER_QUESTION = (re.compile(r"^nameserver:.*\[n\]:?\s*$"), 8)
ADD_NAME_SERVER_QUESTION = (re.compile(r"^Do you wish to add a new nameserver\?.*\[n\]:?\s*$"), 1)
SERVICE_ADDRESS_QUESTION = (re.compile(r"^smnet_dev \(.*\) \[[^\]]*\]:?\s*$"), 1)
SERVICE_GATEWAY_QUESTION = (re.compile(r"^smnet_dflt_gw \(.*\) \[[^\]]*\]:?\s*$"), 1)
CONFIGURE_SUCCESS = "Successfully Applied Changes"
# How long a command may sit on a line that is not a known question before the run gives up.
QUESTION_WAIT_SECONDS = 30
# The line `nss status` and `nss start` print when NSS runs, as the NSS 5.2 image for Azure prints it.
RUNNING_STATUS = re.compile(r"NSS service running with pid \d+")
# NSS is asked for its status this long after `nss start`, in a new session, so a process that
# ended together with the session that started it, or right after its start, is not taken as running.
START_SETTLE_SECONDS = 10
# Error codes of `nss update-now`, from "Troubleshooting Deployed NSS Servers" in the Zscaler help.
UPDATE_ERRORS = {
    96: "the client certificate is invalid",
    97: "a timeout occurred while contacting the upgrade server",
    99: "a problem occurred while downloading and installing the latest version, and 'sudo nss force-update-now' "
    "has to be run",
}
SUDO_PROMPT = "NSS-CERTIFICATE-SUDO-PASSWORD:"
# Every line this script logs starts with this mark. Azure returns the log of a deployment
# script that runs in the Azure CLI container as one line, and the mark shows where a line began.
LOG_MARK = "==> "
# A command that sshd starts does not read the files a login reads, so its PATH can lack the
# directory of the nss command. The PATH of a login shell is asked for with this mark, and
# these directories are looked at when neither PATH has the command.
PATH_MARK = "NSS-CERTIFICATE-LOGIN-PATH="
LOGIN_SHELL_SECONDS = 30
NSS_DIRECTORIES = ("/sc/bin", "/sc/sbin", "/usr/local/bin", "/usr/local/sbin", "/opt/zscaler/bin")
NSS_SERVER_TYPES = (
    "SOFTWARE_AA_FLAG",
    "NSS_FOR_WEB",
    "NSS_FOR_FIREWALL",
    "VZEN",
    "VZEN_SME",
    "VZEN_SMLB",
    "PINNED_NSS",
    "MD5_CAPABLE",
    "ADP",
    "ZIRSVR",
    "NSS_FOR_ZPA",
)
HEALTHY_STATE = "HEALTHY"
# The first zscaler-sdk-python release whose NSS servers API has nss_download_certificate.
MINIMUM_SDK_VERSION = "1.11.5"

ONEAPI_VARIABLES = ("ZSCALER_CLIENT_ID", "ZSCALER_CLIENT_SECRET", "ZSCALER_PRIVATE_KEY", "ZSCALER_VANITY_DOMAIN")
LEGACY_VARIABLES = ("ZIA_USERNAME", "ZIA_PASSWORD", "ZIA_API_KEY", "ZIA_CLOUD")


class AutomationError(Exception):
    """A failure with a message that is safe to show: it never contains a secret."""


def log(message):
    for line in str(message).splitlines() or [""]:
        print(f"{LOG_MARK}{line}", flush=True)


def _flag(env, name, default=False):
    value = (env.get(name) or "").strip().lower()
    if not value:
        return default
    if value in ("true", "1", "yes"):
        return True
    if value in ("false", "0", "no"):
        return False
    raise AutomationError(f"{name} must be true or false.")


def _number(env, name, default):
    value = (env.get(name) or "").strip()
    if not value:
        return default
    if not value.isdigit() or int(value) < 1:
        raise AutomationError(f"{name} must be a positive whole number.")
    return int(value)


def _check_service_interface(address, gateway):
    """The two values are typed into `nss configure`, so only plain IPv4 values are let through."""
    if not address and not gateway:
        return
    if not address or not gateway:
        raise AutomationError(
            "NSS_SERVICE_INTERFACE_ADDRESS and NSS_SERVICE_INTERFACE_GATEWAY must be set together, or not at all."
        )
    try:
        if not re.fullmatch(r"[0-9.]+/[0-9]+", address):
            raise ValueError
        interface = ipaddress.IPv4Interface(address)
    except ValueError:
        raise AutomationError(
            "NSS_SERVICE_INTERFACE_ADDRESS must be an IPv4 address with a prefix length, for example 192.168.100.4/25."
        ) from None
    try:
        if not re.fullmatch(r"[0-9.]+", gateway):
            raise ValueError
        gateway_address = ipaddress.IPv4Address(gateway)
    except ValueError:
        raise AutomationError(
            "NSS_SERVICE_INTERFACE_GATEWAY must be an IPv4 address, for example 192.168.100.1."
        ) from None
    if gateway_address not in interface.network or gateway_address == interface.ip:
        raise AutomationError(
            f"NSS_SERVICE_INTERFACE_GATEWAY ({gateway}) must be another address in the subnet of "
            f"NSS_SERVICE_INTERFACE_ADDRESS ({interface.network})."
        )


def read_settings(env=None):
    """Reads and validates the environment variables. Empty values count as not set."""
    env = os.environ if env is None else env

    def text(name, default=""):
        return (env.get(name) or "").strip() or default

    settings = {
        "vm_host": text("NSS_VM_HOST"),
        "vm_port": _number(env, "NSS_VM_PORT", 22),
        "vm_username": text("NSS_VM_USERNAME", "zsroot"),
        # Passwords and keys are taken as they are; only a missing value is rejected.
        "vm_password": env.get("NSS_VM_PASSWORD") or "",
        "host_key_fingerprint": text("NSS_SSH_HOST_KEY_FINGERPRINT"),
        "ssh_wait_seconds": _number(env, "NSS_SSH_WAIT_SECONDS", 900),
        "command_timeout_seconds": _number(env, "NSS_COMMAND_TIMEOUT_SECONDS", 300),
        "server_name": text("NSS_SERVER_NAME"),
        "server_type": text("NSS_SERVER_TYPE", "NSS_FOR_WEB"),
        "allow_healthy_server": _flag(env, "NSS_ALLOW_HEALTHY_SERVER"),
        "force_install": _flag(env, "NSS_FORCE_CERTIFICATE_INSTALL"),
        "print_dump_config": _flag(env, "NSS_PRINT_DUMP_CONFIG", default=True),
        "service_interface_address": text("NSS_SERVICE_INTERFACE_ADDRESS"),
        "service_interface_gateway": text("NSS_SERVICE_INTERFACE_GATEWAY"),
        "start_nss": _flag(env, "NSS_START"),
        "update_timeout_seconds": _number(env, "NSS_UPDATE_TIMEOUT_SECONDS", 600),
        "enable_autostart": _flag(env, "NSS_ENABLE_AUTOSTART"),
        "client_id": text("ZSCALER_CLIENT_ID"),
        "client_secret": env.get("ZSCALER_CLIENT_SECRET") or "",
        "private_key": env.get("ZSCALER_PRIVATE_KEY") or "",
        "vanity_domain": text("ZSCALER_VANITY_DOMAIN"),
        "cloud": text("ZSCALER_CLOUD"),
        "zia_username": text("ZIA_USERNAME"),
        "zia_password": env.get("ZIA_PASSWORD") or "",
        "zia_api_key": env.get("ZIA_API_KEY") or "",
        "zia_cloud": text("ZIA_CLOUD"),
        "output_path": text("AZ_SCRIPTS_OUTPUT_PATH"),
    }

    missing = [
        name
        for name, key in (
            ("NSS_VM_HOST", "vm_host"),
            ("NSS_VM_PASSWORD", "vm_password"),
            ("NSS_SERVER_NAME", "server_name"),
        )
        if not settings[key]
    ]

    if settings["client_id"]:
        settings["auth"] = "oneapi"
        if not settings["vanity_domain"]:
            missing.append("ZSCALER_VANITY_DOMAIN")
        if not settings["client_secret"] and not settings["private_key"]:
            missing.append("ZSCALER_CLIENT_SECRET or ZSCALER_PRIVATE_KEY")
    elif settings["zia_username"]:
        settings["auth"] = "legacy"
        missing.extend(
            name
            for name, key in (
                ("ZIA_PASSWORD", "zia_password"),
                ("ZIA_API_KEY", "zia_api_key"),
                ("ZIA_CLOUD", "zia_cloud"),
            )
            if not settings[key]
        )
    else:
        settings["auth"] = None
        missing.append(
            "Zscaler credentials: either OneAPI (ZSCALER_CLIENT_ID, ZSCALER_VANITY_DOMAIN and "
            "ZSCALER_CLIENT_SECRET or ZSCALER_PRIVATE_KEY) or the legacy ZIA API "
            "(ZIA_USERNAME, ZIA_PASSWORD, ZIA_API_KEY and ZIA_CLOUD)"
        )

    if missing:
        raise AutomationError("Required settings are missing: " + "; ".join(missing) + ".")

    if settings["server_type"] not in NSS_SERVER_TYPES:
        raise AutomationError("NSS_SERVER_TYPE must be one of: " + ", ".join(NSS_SERVER_TYPES) + ".")

    _check_service_interface(settings["service_interface_address"], settings["service_interface_gateway"])

    # The user name is used in a remote path, so it is limited to characters that need no quoting.
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*", settings["vm_username"]):
        raise AutomationError("NSS_VM_USERNAME is not a valid user name.")

    return settings


# --------------------------------------------------------------------------------------
# ZIA
# --------------------------------------------------------------------------------------


def connect_zia(settings):
    """Returns a zscaler-sdk-python client for the credentials that are set."""
    logging_off = {"enabled": False, "verbose": False}

    if settings["auth"] == "legacy":
        from zscaler.oneapi_client import LegacyZIAClient

        log("Authenticating to ZIA with the legacy API (ZIA_USERNAME, ZIA_PASSWORD, ZIA_API_KEY, ZIA_CLOUD).")
        return LegacyZIAClient(
            {
                "username": settings["zia_username"],
                "password": settings["zia_password"],
                "api_key": settings["zia_api_key"],
                "cloud": settings["zia_cloud"],
                "logging": logging_off,
            }
        )

    from zscaler import ZscalerClient

    if settings["zia_username"]:
        log("Both OneAPI and legacy ZIA credentials are set. OneAPI is used.")
    log("Authenticating to Zscaler with OneAPI (ZSCALER_CLIENT_ID and ZSCALER_VANITY_DOMAIN).")

    config = {
        "clientId": settings["client_id"],
        "vanityDomain": settings["vanity_domain"],
        "logging": logging_off,
    }
    if settings["client_secret"]:
        config["clientSecret"] = settings["client_secret"]
    else:
        config["privateKey"] = settings["private_key"]
    if settings["cloud"]:
        config["cloud"] = settings["cloud"]
    return ZscalerClient(config)


def find_nss_server(client, name):
    """Returns the NSS server object with exactly this name, or None."""
    servers, _, error = client.zia.nss_servers.list_nss_servers()
    if error:
        raise AutomationError(f"Listing the NSS servers in ZIA failed: {error}")

    matches = [server for server in servers or [] if _field(server, "name") == name]
    if len(matches) > 1:
        raise AutomationError(f"ZIA has {len(matches)} NSS servers named '{name}'. Use a name that is unique.")
    return matches[0] if matches else None


def create_nss_server(client, name, server_type):
    server, _, error = client.zia.nss_servers.add_nss_server(name=name, status="ENABLED", type=server_type)
    if error:
        raise AutomationError(f"Creating the NSS server '{name}' in ZIA failed: {error}")
    if server is None or _field(server, "id") is None:
        raise AutomationError(f"ZIA did not return an ID for the new NSS server '{name}'.")
    return server


def download_certificate(client, nss_id):
    """Returns the certificate ZIP file of the NSS server as bytes, downloaded by the SDK."""
    download = getattr(client.zia.nss_servers, "nss_download_certificate", None)
    if not callable(download):
        raise AutomationError(
            f"The installed zscaler-sdk-python{_installed_sdk_version()} has no nss_download_certificate. "
            f"Install zscaler-sdk-python {MINIMUM_SDK_VERSION} or later."
        )

    content, _, error = download(nss_id)

    if error:
        raise AutomationError(f"Downloading the certificate of NSS server {nss_id} failed: {error}")
    if not content:
        raise AutomationError(f"ZIA returned no certificate for NSS server {nss_id}.")
    return bytes(content)


def _installed_sdk_version():
    try:
        from importlib.metadata import version

        return f" ({version('zscaler-sdk-python')})"
    except Exception:
        return ""


def certificate_digest(content):
    """Validates the downloaded bytes and returns a digest of the files inside the ZIP archive.

    The digest covers the member names and contents only, so it stays the same when ZIA builds
    the archive again for the same certificate.
    """
    if not zipfile.is_zipfile(io.BytesIO(content)):
        raise AutomationError(
            f"ZIA returned {len(content)} bytes that are not a ZIP archive, so nothing was copied to the virtual machine."
        )

    digest = hashlib.sha256()
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = sorted(info.filename for info in archive.infolist() if not info.is_dir())
            if not names:
                raise AutomationError("The certificate ZIP archive returned by ZIA is empty.")
            for name in names:
                digest.update(name.encode("utf-8") + b"\0")
                digest.update(hashlib.sha256(archive.read(name)).digest())
    except zipfile.BadZipFile as error:
        raise AutomationError(f"The certificate ZIP archive returned by ZIA is damaged: {error}") from error
    return digest.hexdigest()


def _field(item, name):
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


# --------------------------------------------------------------------------------------
# NSS virtual machine
# --------------------------------------------------------------------------------------


class NssVm:
    """SSH session to the NSS virtual machine, authenticated with the password of the account."""

    def __init__(self, settings):
        self._host = settings["vm_host"]
        self._port = settings["vm_port"]
        self._username = settings["vm_username"]
        self._password = settings["vm_password"]
        self._expected_fingerprint = settings["host_key_fingerprint"]
        self._wait_seconds = settings["ssh_wait_seconds"]
        self._command_timeout = settings["command_timeout_seconds"]
        self._transport = None
        self.home = f"/home/{self._username}"
        # Set by locate_nss: the nss command with its directory, and the PATH it is run with.
        self.nss = "nss"
        self.nss_search_path = None

    def connect(self):
        """Waits until SSH answers, then authenticates. A rejected password is not retried."""
        import paramiko

        deadline = time.monotonic() + self._wait_seconds
        attempt = 0
        while True:
            attempt += 1
            transport = None
            try:
                connection = socket.create_connection((self._host, self._port), timeout=15)
                transport = paramiko.Transport(connection)
                transport.banner_timeout = 30
                transport.start_client(timeout=30)
                self._check_host_key(transport.get_remote_server_key())
                self._authenticate(transport, paramiko)
                transport.set_keepalive(30)
                self._transport = transport
                return
            except paramiko.AuthenticationException as error:
                self._close(transport)
                raise AutomationError(
                    f"The virtual machine rejected the password of '{self._username}' ({type(error).__name__}). "
                    "Check NSS_VM_PASSWORD."
                ) from None
            except AutomationError:
                self._close(transport)
                raise
            except (OSError, EOFError, paramiko.SSHException) as error:
                self._close(transport)
                if time.monotonic() >= deadline:
                    raise AutomationError(
                        f"SSH to {self._host}:{self._port} did not answer within {self._wait_seconds} seconds "
                        f"({type(error).__name__}: {error})."
                    ) from None
                if attempt == 1:
                    log(f"Waiting for SSH on {self._host}:{self._port} ...")
                time.sleep(10)

    def _check_host_key(self, key):
        fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode("ascii").rstrip("=")
        if not self._expected_fingerprint:
            log(f"SSH host key {key.get_name()} {fingerprint} (not verified: NSS_SSH_HOST_KEY_FINGERPRINT is not set).")
            return
        if fingerprint.rstrip("=") != self._expected_fingerprint.rstrip("="):
            raise AutomationError(
                f"The SSH host key of {self._host} is {fingerprint}, which is not the fingerprint in "
                "NSS_SSH_HOST_KEY_FINGERPRINT. Nothing was sent to the virtual machine."
            )
        log(f"SSH host key {fingerprint} matches NSS_SSH_HOST_KEY_FINGERPRINT.")

    def _authenticate(self, transport, paramiko):
        password = self._password

        def answer(title, instructions, prompts):
            return [password for _ in prompts]

        try:
            transport.auth_password(self._username, password, fallback=False)
        except paramiko.BadAuthenticationType as error:
            if "keyboard-interactive" not in error.allowed_types:
                raise AutomationError(
                    "The virtual machine accepts neither password nor keyboard-interactive authentication "
                    f"(allowed: {', '.join(error.allowed_types)})."
                ) from None
            transport.auth_interactive(self._username, answer)
        except paramiko.AuthenticationException:
            # Some servers offer the password method but only accept the password interactively.
            if not transport.is_active():
                raise
            transport.auth_interactive(self._username, answer)

        if not transport.is_authenticated():
            raise paramiko.AuthenticationException("Authentication is incomplete.")

    @staticmethod
    def _close(transport):
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass

    def close(self):
        self._close(self._transport)
        self._transport = None

    def run(self, command, input_bytes=None, sudo=False, answers=None, timeout=None):
        """Runs a command and returns (exit status, output). With sudo, the command must contain SUDO_PROMPT.

        `timeout` is the number of seconds the command may take, when that is not the usual time.

        `answers` is for a command that asks questions: a list of ((pattern, most times), reply). A
        question is the last line of the output, without a line end. A known question gets its reply.
        The run stops when a question is asked more often than expected, which means the reply was
        not accepted, and when the command waits on a line that is no known question. It never guesses.
        """
        channel = self._transport.open_session(timeout=30)
        try:
            if sudo:
                # sudo may require a terminal, and with one the prompt arrives in the output.
                channel.get_pty(term="dumb", width=200, height=50)
            channel.exec_command(command)

            if input_bytes is not None:
                channel.sendall(input_bytes)
                channel.shutdown_write()

            output = bytearray()
            checked = 0
            password_sent = False
            prompt = SUDO_PROMPT.encode("ascii")
            timeout = timeout or self._command_timeout
            deadline = time.monotonic() + timeout
            answered = 0
            asked = [0 for _ in answers or []]
            last_received = time.monotonic()

            while True:
                received = False
                if channel.recv_ready():
                    output.extend(channel.recv(65536))
                    received = True
                if channel.recv_stderr_ready():
                    output.extend(channel.recv_stderr(65536))
                    received = True

                if sudo and prompt in output[checked:]:
                    checked = output.index(prompt, checked) + len(prompt)
                    if password_sent:
                        raise AutomationError(
                            f"sudo rejected the password of '{self._username}' on the virtual machine."
                        )
                    channel.sendall(self._password.encode("utf-8") + b"\n")
                    password_sent = True
                    answered = len(output)

                if received:
                    last_received = time.monotonic()
                question = self._last_line(output[answered:]) if answers else ""
                if question and received:
                    for index, ((pattern, most), reply) in enumerate(answers):
                        if pattern.search(question):
                            asked[index] += 1
                            if asked[index] > most:
                                raise AutomationError(
                                    "The virtual machine asked a question again after it was answered, so it did not "
                                    f"accept the answer '{reply}'. Output so far:\n" + self._readable(output)
                                )
                            channel.sendall(reply.encode("ascii") + b"\n")
                            answered = len(output)
                            break

                if not received:
                    if question and time.monotonic() - last_received >= QUESTION_WAIT_SECONDS:
                        raise AutomationError(
                            f"A command on the virtual machine waits for input after '{question.strip()}', which is "
                            "not a question this script knows. Nothing was answered to it. Output so far:\n"
                            + self._readable(output)
                        )
                    if channel.exit_status_ready():
                        # Output that arrived together with the exit status is still buffered.
                        while channel.recv_ready():
                            output.extend(channel.recv(65536))
                        while channel.recv_stderr_ready():
                            output.extend(channel.recv_stderr(65536))
                        break
                    if time.monotonic() >= deadline:
                        raise AutomationError(
                            f"A command on the virtual machine did not finish within {timeout} seconds. "
                            "Output so far:\n" + self._readable(output)
                        )
                    time.sleep(0.05)

            return channel.recv_exit_status(), self._readable(output)
        finally:
            channel.close()

    @staticmethod
    def _last_line(output):
        """The text after the last line end: where a command that asks a question waits."""
        return bytes(output).decode("utf-8", errors="replace").replace("\r", "\n").rsplit("\n", 1)[-1]

    def login_path(self):
        """Returns the PATH of a login shell on the virtual machine, or None when it cannot be read."""
        import paramiko

        answer = re.compile(re.escape(PATH_MARK).encode("ascii") + rb"(/[^\r\n]*)\r?\n")
        channel = None
        try:
            channel = self._transport.open_session(timeout=30)
            channel.get_pty(term="dumb", width=500, height=50)
            channel.invoke_shell()
            output = bytearray()
            asked = 0
            quiet_since = time.monotonic()
            deadline = quiet_since + LOGIN_SHELL_SECONDS
            while time.monotonic() < deadline:
                if channel.recv_ready():
                    output.extend(channel.recv(65536))
                    quiet_since = time.monotonic()
                    found = answer.search(output)
                    if found:
                        path = found.group(1).decode("utf-8", errors="replace").strip()
                        return path if re.fullmatch(r"[A-Za-z0-9_./:~+@-]+", path) else None
                elif channel.exit_status_ready():
                    return None
                elif asked < 2 and time.monotonic() - quiet_since >= (1 if asked == 0 else 8):
                    # The shell has printed what it prints at a login and waits for input. The line the
                    # terminal echoes back has $PATH after the mark, the answer has the value. It is
                    # asked a second time in case the login discarded what was typed too early.
                    channel.sendall(f'echo "{PATH_MARK}$PATH"\n'.encode("ascii"))
                    asked += 1
                    quiet_since = time.monotonic()
                else:
                    time.sleep(0.05)
            return None
        except (paramiko.SSHException, OSError, EOFError):
            return None
        finally:
            if channel is not None:
                channel.close()

    def locate_nss(self):
        """Finds the nss command, so that sudo can be given its full path, and says where it was found."""
        status, output = self.run("sh -c 'command -v nss'")
        if status == 0 and output.startswith("/"):
            self.nss = output.splitlines()[-1].strip()
            log(f"The nss command is {self.nss}.")
            return

        status, output = self.run("sh -c 'echo $PATH'")
        session_path = output.strip() if status == 0 else ""
        login_path = self.login_path()
        if login_path:
            status, output = self.run(f"env PATH='{login_path}' sh -c 'command -v nss'")
            if status == 0 and output.startswith("/"):
                self.nss = output.splitlines()[-1].strip()
                self.nss_search_path = login_path
                log(f"The nss command is {self.nss}, found with the PATH of a login shell.")
                return

        candidates = " ".join(f"{directory}/nss" for directory in NSS_DIRECTORIES)
        status, output = self.run(
            f"sh -c 'for f in {candidates}; do if [ -x $f ]; then echo $f; exit 0; fi; done; exit 1'"
        )
        if status == 0 and output.startswith("/"):
            self.nss = output.splitlines()[-1].strip()
            known_path = login_path or session_path
            self.nss_search_path = ":".join(part for part in (os.path.dirname(self.nss), known_path) if part)
            if not re.fullmatch(r"[A-Za-z0-9_./:~+@-]+", self.nss_search_path):
                self.nss_search_path = os.path.dirname(self.nss)
            log(
                f"The nss command is {self.nss}. No PATH has it; it was found by looking in {os.path.dirname(self.nss)}."
            )
            return

        raise AutomationError(
            "The nss command was not found on the virtual machine. "
            f"PATH of the SSH session: {session_path or 'could not be read'}. "
            f"PATH of a login shell: {login_path or 'could not be read'}. "
            f"Also looked in: {', '.join(NSS_DIRECTORIES)}. "
            "Nothing was created or changed in ZIA."
        )

    def sudo_nss(self, arguments):
        """The command line that runs `sudo nss <arguments>` the way a login shell would find it."""
        environment = f"env PATH='{self.nss_search_path}' " if self.nss_search_path else ""
        return f"{environment}sudo -p '{SUDO_PROMPT}' {self.nss} {arguments}"

    def _readable(self, output):
        text = bytes(output).decode("utf-8", errors="replace").replace("\r\n", "\n")
        text = text.replace(SUDO_PROMPT, "")
        if self._password:
            text = text.replace(self._password, "********")
        return text.strip()

    def upload(self, content, remote_path):
        """Copies bytes to a file on the virtual machine, readable by its owner only."""
        try:
            sftp = self._transport.open_sftp_client()
        except Exception as error:
            log(f"SFTP is not available ({type(error).__name__}); copying the file through the SSH session instead.")
            sftp = None

        if sftp is not None:
            try:
                sftp.putfo(io.BytesIO(content), remote_path)
                sftp.chmod(remote_path, 0o600)
                size = sftp.stat(remote_path).st_size
            finally:
                sftp.close()
        else:
            status, output = self.run(f"cat > {remote_path} && chmod 600 {remote_path}", input_bytes=content)
            if status != 0:
                raise AutomationError(f"Copying the file to {remote_path} failed (exit status {status}):\n{output}")
            status, output = self.run(f"wc -c < {remote_path}")
            size = int(output.split()[0]) if status == 0 and output.split() and output.split()[0].isdigit() else -1

        if size != len(content):
            raise AutomationError(
                f"{remote_path} on the virtual machine has {size} bytes, but {len(content)} bytes were sent."
            )

    def read_text(self, remote_path):
        """Returns the content of a small text file, or None when it cannot be read."""
        status, output = self.run(f"cat {remote_path}")
        return output if status == 0 else None


def install_certificate(vm, content, print_dump_config):
    """Follows the Zscaler guide: copy the ZIP file to the home directory, install it, check the configuration."""
    remote_path = f"{vm.home}/{CERTIFICATE_FILE_NAME}"

    log(f"Copying {CERTIFICATE_FILE_NAME} ({len(content)} bytes) to {remote_path}.")
    vm.upload(content, remote_path)

    log(f"Running: sudo nss install-cert {CERTIFICATE_FILE_NAME}")
    status, output = vm.run(f"cd {vm.home} && {vm.sudo_nss(f'install-cert {CERTIFICATE_FILE_NAME}')}", sudo=True)
    if output:
        log(output)
    if status != 0:
        # The last lines of the output are repeated in the error, which outlives the script log.
        last_lines = [line.strip() for line in output.splitlines() if line.strip()][-3:]
        raise AutomationError(
            f"'sudo nss install-cert {CERTIFICATE_FILE_NAME}' failed with exit status {status}."
            + (f" Last output: {' / '.join(last_lines)}" if last_lines else "")
        )

    log("Running: sudo nss dump-config")
    status, output = vm.run(vm.sudo_nss("dump-config"), sudo=True)
    if status != 0:
        raise AutomationError(
            f"The certificate was installed, but 'sudo nss dump-config' failed with exit status {status}:\n{output}"
        )
    if print_dump_config:
        log(output)
    else:
        log(
            f"'sudo nss dump-config' succeeded ({len(output.splitlines())} lines). The output is not shown here; "
            "run the command on the virtual machine to review it."
        )


def write_marker(vm, marker, file_name=MARKER_FILE_NAME):
    status, output = vm.run(f"echo '{marker}' > {vm.home}/{file_name}")
    if status != 0:
        log(f"Warning: the marker file {file_name} was not written (exit status {status}): {output}")


def configured_service_interface(dump_config):
    """Returns (address, gateway) as `nss dump-config` shows them; a value it does not show is None."""
    values = []
    for key in ("smnet_dev", "smnet_dflt_gw"):
        found = re.search(rf"^\s*{key}\s*[:=]\s*(\S+)\s*$", dump_config, re.MULTILINE)
        values.append(found.group(1) if found else None)
    return tuple(values)


def configure_service_interface(vm, address, gateway):
    """Answers `sudo nss configure` with the service interface values. Returns False when they are already set."""
    wanted = (address, gateway)
    marker = f"{address} {gateway}"
    if (vm.read_text(f"{vm.home}/{CONFIGURE_MARKER_FILE_NAME}") or "").strip() == marker:
        log(
            f"The service interface was configured with {address} and gateway {gateway} by an earlier run. Nothing to do."
        )
        return False

    status, output = vm.run(vm.sudo_nss("dump-config"), sudo=True)
    if status == 0 and configured_service_interface(output) == wanted:
        log(f"The service interface already has {address} and gateway {gateway}. Nothing to do.")
        write_marker(vm, marker, CONFIGURE_MARKER_FILE_NAME)
        return False

    log(f"Running: sudo nss configure (service interface {address}, gateway {gateway}; name servers are not changed)")
    status, output = vm.run(
        vm.sudo_nss("configure"),
        sudo=True,
        answers=[
            (NAME_SERVER_QUESTION, "n"),
            (ADD_NAME_SERVER_QUESTION, "n"),
            (SERVICE_ADDRESS_QUESTION, address),
            (SERVICE_GATEWAY_QUESTION, gateway),
        ],
    )
    if output:
        log(output)
    last_lines = " / ".join([line.strip() for line in output.splitlines() if line.strip()][-3:])
    if status != 0:
        raise AutomationError(f"'sudo nss configure' failed with exit status {status}. Last output: {last_lines}")

    status, check = vm.run(vm.sudo_nss("dump-config"), sudo=True)
    shown = configured_service_interface(check) if status == 0 else (None, None)
    if None not in shown:
        if shown != wanted:
            raise AutomationError(
                "'sudo nss configure' ended without an error, but 'sudo nss dump-config' shows the service interface "
                f"as {shown[0]} with gateway {shown[1]}, not {address} with gateway {gateway}."
            )
        log("'sudo nss dump-config' shows the new values.")
    elif CONFIGURE_SUCCESS in output:
        log(
            "'sudo nss dump-config' does not show the service interface in a form this script reads; "
            f"'{CONFIGURE_SUCCESS}' from nss configure is taken as the result."
        )
    else:
        raise AutomationError(
            f"'sudo nss configure' ended without '{CONFIGURE_SUCCESS}', and 'sudo nss dump-config' does not show the "
            f"service interface. Last output: {last_lines}"
        )

    write_marker(vm, marker, CONFIGURE_MARKER_FILE_NAME)
    return True


def nss_status(vm):
    """Runs `sudo nss status` and returns (True when it shows NSS running, output)."""
    log("Running: sudo nss status")
    _, output = vm.run(vm.sudo_nss("status"), sudo=True)
    if output:
        log(output)
    return bool(RUNNING_STATUS.search(output)), output


def start_nss(vm, update_timeout):
    """Follows the Zscaler guide: download the NSS binaries, start NSS, check that it runs.

    Returns False when NSS already ran. Raises AutomationError when NSS does not run at the end.
    `sudo nss status` decides that, and not the exit status of `update-now` or `start`: what the
    two commands return when there is nothing to update, or nothing to start, is not known.
    """
    running, _ = nss_status(vm)
    if running:
        log("NSS is already running. Nothing to update or start.")
        return False

    log("Running: sudo nss update-now (downloads and installs the NSS binaries, which can take several minutes)")
    update_status, output = vm.run(vm.sudo_nss("update-now"), sudo=True, timeout=update_timeout)
    if output:
        log(output)
    update_result = ""
    if update_status != 0:
        update_result = f"'sudo nss update-now' ended with exit status {update_status}"
        if update_status in UPDATE_ERRORS:
            update_result += f", which Zscaler documents as: {UPDATE_ERRORS[update_status]}"
        update_result += ". "
        log(update_result + "NSS is started all the same, in case the binaries are installed.")

    log("Running: sudo nss start")
    start_status, start_output = vm.run(vm.sudo_nss("start"), sudo=True)
    if start_output:
        log(start_output)

    time.sleep(START_SETTLE_SECONDS)
    running, status_output = nss_status(vm)
    if not running:
        last_lines = " / ".join(
            [line.strip() for line in (status_output or start_output).splitlines() if line.strip()][-3:]
        )
        raise AutomationError(
            f"{update_result}'sudo nss start' ended with exit status {start_status}, and 'sudo nss status' does not "
            f"show NSS running. Last output: {last_lines or 'none'}"
        )
    return True


def enable_autostart(vm):
    """Makes NSS start when the virtual machine starts. It does not start NSS now."""
    log("Running: sudo nss enable-autostart")
    status, output = vm.run(vm.sudo_nss("enable-autostart"), sudo=True)
    if output:
        log(output)
    if status != 0:
        last_lines = " / ".join([line.strip() for line in output.splitlines() if line.strip()][-3:])
        raise AutomationError(
            f"'sudo nss enable-autostart' failed with exit status {status}. Last output: {last_lines}"
        )


# --------------------------------------------------------------------------------------
# Workflow
# --------------------------------------------------------------------------------------


def run(settings, zia_client_factory=None, vm_factory=None):
    """Runs the workflow and returns the values reported as deployment script outputs."""
    zia_client_factory = zia_client_factory or connect_zia
    vm_factory = vm_factory or NssVm
    name = settings["server_name"]

    # The virtual machine is contacted first, so an unreachable machine, a wrong password or a
    # machine without the nss command stops the run before anything is created in ZIA.
    vm = vm_factory(settings)
    log(f"Connecting to {settings['vm_username']}@{settings['vm_host']} over SSH.")
    vm.connect()

    client = None
    created = False
    configured = False
    running = False
    try:
        vm.locate_nss()
        # The service interface is configured before the certificate is installed, and before ZIA
        # is contacted: a failure here leaves nothing behind in ZIA either.
        if settings["service_interface_address"]:
            configured = configure_service_interface(
                vm, settings["service_interface_address"], settings["service_interface_gateway"]
            )
        else:
            log("NSS_SERVICE_INTERFACE_ADDRESS is not set: 'nss configure' is left to you.")
        installed_marker = (vm.read_text(f"{vm.home}/{MARKER_FILE_NAME}") or "").strip()

        client = zia_client_factory(settings)
        server = find_nss_server(client, name)
        if server is None:
            log(f"Creating the NSS server '{name}' ({settings['server_type']}) in ZIA.")
            server = create_nss_server(client, name, settings["server_type"])
            created = True
        else:
            log(f"The NSS server '{name}' already exists in ZIA and is reused.")

        nss_id = str(_field(server, "id"))
        if not nss_id.isdigit():
            raise AutomationError("ZIA returned an NSS server ID that is not a number.")
        state = _field(server, "state")
        log(f"NSS server ID: {nss_id}. State reported by ZIA: {state or 'not reported'}.")

        if created:
            log("The new NSS server is a pending change in ZIA. This script does not activate it; activate it in ZIA.")

        try:
            content = download_certificate(client, nss_id)
        except AutomationError as error:
            if created:
                raise AutomationError(
                    f"{error} The NSS server was created in this run and is not activated. If ZIA only issues the "
                    "certificate after activation, activate the pending change in ZIA and run this again; the "
                    "server is then reused."
                ) from None
            raise
        marker = f"{nss_id} {certificate_digest(content)}"

        if installed_marker == marker and not settings["force_install"]:
            log("This certificate is already installed on the virtual machine. Nothing to do.")
            installed = False
        else:
            # The guide allows the certificate on one active NSS virtual machine only. A reused server
            # object that ZIA reports as healthy is in use by a machine that is not known to be this one.
            if not created and state == HEALTHY_STATE and not settings["allow_healthy_server"]:
                raise AutomationError(
                    f"ZIA reports the NSS server '{name}' as {HEALTHY_STATE}, so its certificate is in use by an NSS "
                    "virtual machine, and this run cannot tell that it is this one. The certificate must be installed "
                    "on only one active NSS virtual machine. Use another NSS server name, or allow the reuse explicitly "
                    "(allowHealthyNssServer in the template, NSS_ALLOW_HEALTHY_SERVER=true for the script)."
                )
            install_certificate(vm, content, settings["print_dump_config"])
            write_marker(vm, marker)
            installed = True
            log("The NSS certificate is installed.")

        if settings["start_nss"]:
            # Everything before this is done and stays done, so NSS that does not start ends the run
            # with a warning and nssRunning false, not with an error.
            try:
                start_nss(vm, settings["update_timeout_seconds"])
                running = True
                log("NSS is running.")
            except AutomationError as error:
                log(f"WARNING: NSS is not running. {error}")
                if created:
                    log(
                        "This run created the NSS server object, which is a pending change until it is activated "
                        "in ZIA. If that is why NSS did not start, activate it first."
                    )
                log(
                    "The certificate is installed. To start NSS, run 'sudo nss update-now', 'sudo nss start' and "
                    "'sudo nss status' on the virtual machine, or run this again."
                )
        else:
            log(
                "NSS_START is not set, so NSS is not started. "
                "That is 'sudo nss update-now' and then 'sudo nss start' on the virtual machine."
            )

        if settings["enable_autostart"]:
            enable_autostart(vm)
    finally:
        vm.close()
        # The Zscaler session is deliberately not closed (no `with`, no __exit__, no deauthenticate):
        # the SDK ends a ZIA session that made a change in order to activate the staged configuration,
        # and activation is left to the user. The session expires on its own.

    return {
        "nssServerId": nss_id,
        "nssServerName": name,
        "nssServerCreated": created,
        "certificateInstalled": installed,
        "serviceInterfaceConfigured": configured,
        "nssRunning": running,
        "autostartEnabled": settings["enable_autostart"],
    }


SECRET_SETTINGS = ("vm_password", "client_secret", "private_key", "zia_password", "zia_api_key")


def scrub(text, settings):
    """Removes the secret values from a message produced by a library."""
    for key in SECRET_SETTINGS:
        secret = (settings or {}).get(key)
        if secret:
            text = text.replace(secret, "********")
    return text


def main():
    settings = None
    try:
        settings = read_settings()
        outputs = run(settings)
        if settings["output_path"]:
            with open(settings["output_path"], "w", encoding="utf-8") as output_file:
                json.dump(outputs, output_file)
        log(json.dumps(outputs))
    except AutomationError as error:
        return fail(f"ERROR: {error}", settings)
    except Exception as error:
        # No traceback: only the kind of failure and its message, without the secret values.
        return fail(f"ERROR: {type(error).__name__}: {error}", settings)
    return 0


def fail(message, settings):
    """Prints the error as one line on the error output, which Azure reports as the deployment error."""
    # A log line of its own, so the error is not glued to the line before it in a log that
    # Azure returns as one line.
    log("The run failed.")
    print(scrub(" ".join(message.split()), settings), file=sys.stderr, flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
