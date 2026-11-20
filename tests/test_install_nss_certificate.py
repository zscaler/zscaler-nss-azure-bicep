"""Tests for scripts/install-nss-certificate.py.

Nothing here talks to Zscaler or to a real NSS virtual machine. ZIA is a fake client, or the real
zscaler-sdk-python with its HTTP calls intercepted, and the virtual machine is a local paramiko
SSH server that imitates the few commands the script runs. The fake proves the SSH plumbing
(authentication, sudo prompt, file copy, exit codes), not the behaviour of the NSS appliance.

Run with:  python -m pytest tests
Needs:     pytest, paramiko; the tests that use the real SDK also need zscaler-sdk-python 1.11.5 or later
           and responses.
"""

import importlib.util
import io
import os
import re
import socket
import threading
import time
import zipfile
from pathlib import Path
from unittest.mock import Mock

import paramiko
import pytest

from non_secrets import (
    NOT_A_ACCESS_TOKEN,
    NOT_A_CLIENT_ID,
    NOT_A_OTHER_SUDO_PASSWORD,
    NOT_A_SECRET,
    NOT_A_WRONG_VM_PASSWORD,
)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install-nss-certificate.py"
spec = importlib.util.spec_from_file_location("install_nss_certificate", SCRIPT)
automation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(automation)

VM_PASSWORD = NOT_A_SECRET

# The NSS server types that are documented and supported. The script must allow exactly these.
SUPPORTED_SERVER_TYPES = (
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
HOST_KEY = paramiko.RSAKey.generate(2048)


def certificate_zip(body="certificate-1"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("nss.crt", body)
        archive.writestr("nss.key", "key")
    return buffer.getvalue()


# --------------------------------------------------------------------------------------
# Fake NSS virtual machine
# --------------------------------------------------------------------------------------


class FakeSftp(paramiko.SFTPServerInterface):
    """Just enough SFTP for putfo, chmod and stat, rooted in the fake home directory."""

    def __init__(self, server, *args, **kwargs):
        super().__init__(server, *args, **kwargs)
        self.vm = server.vm

    def _local(self, path):
        return self.vm.local_path(path)

    def stat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(os.stat(self._local(path)))
        except OSError as error:
            return paramiko.SFTPServer.convert_errno(error.errno)

    lstat = stat

    def chattr(self, path, attr):
        if attr.st_mode is not None:
            os.chmod(self._local(path), attr.st_mode)
        return paramiko.SFTP_OK

    def open(self, path, flags, attr):
        try:
            descriptor = os.open(self._local(path), flags, 0o600)
        except OSError as error:
            return paramiko.SFTPServer.convert_errno(error.errno)
        mode = "wb" if flags & os.O_WRONLY else "r+b" if flags & os.O_RDWR else "rb"
        handle = paramiko.SFTPHandle(flags)
        handle.filename = self._local(path)
        handle.readfile = handle.writefile = os.fdopen(descriptor, mode)
        self.vm.sftp_uploads += 1
        return handle


class FakeSshServer(paramiko.ServerInterface):
    def __init__(self, vm):
        self.vm = vm
        self.has_pty = False

    def get_allowed_auths(self, username):
        return ",".join(self.vm.auth_methods)

    def check_auth_none(self, username):
        return paramiko.AUTH_FAILED

    def check_auth_password(self, username, password):
        if "password" in self.vm.working_auth_methods and password == self.vm.password:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_interactive(self, username, submethods):
        if "keyboard-interactive" not in self.vm.auth_methods:
            return paramiko.AUTH_FAILED
        query = paramiko.InteractiveQuery("", "")
        query.add_prompt("Password for zsroot@nss:", False)
        return query

    def check_auth_interactive_response(self, responses):
        if "keyboard-interactive" in self.vm.working_auth_methods and responses == [self.vm.password]:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(self, channel, term, width, height, pixelwidth, pixelheight, modes):
        self.has_pty = True
        return True

    def check_channel_exec_request(self, channel, command):
        has_pty, self.has_pty = self.has_pty, False
        threading.Thread(target=self.vm.execute, args=(channel, command.decode(), has_pty), daemon=True).start()
        return True

    def check_channel_shell_request(self, channel):
        if not self.vm.login_shell:
            return False
        threading.Thread(target=self.vm.shell, args=(channel,), daemon=True).start()
        return True

    def check_channel_subsystem_request(self, channel, name):
        if name == "sftp" and not self.vm.sftp:
            return False
        return super().check_channel_subsystem_request(channel, name)


SESSION_PATH = "/sbin:/bin:/usr/sbin:/usr/bin:/usr/local/sbin:/usr/local/bin:/home/zsroot/bin"


class FakeNssVm:
    """A local SSH server with a home directory and a pretend `nss` command behind sudo.

    As on the NSS image, a command that sshd starts does not find `nss`: its directory is only
    in the PATH of a login shell.
    """

    def __init__(self, home, sftp=False, auth_methods=("keyboard-interactive",), working_auth_methods=None):
        self.home = Path(home)
        self.password = VM_PASSWORD
        self.sudo_password = VM_PASSWORD
        self.sudo_needs_password = True
        self.sftp = sftp
        self.sftp_uploads = 0
        self.auth_methods = auth_methods
        self.working_auth_methods = auth_methods if working_auth_methods is None else working_auth_methods
        self.install_exit_status = 0
        self.session_path = SESSION_PATH
        self.login_path = SESSION_PATH + ":/sc/bin"
        self.nss_directory = "/sc/bin"
        self.login_shell = True
        self.typed = []
        # What `nss configure` keeps, and how the command behaves. The questions are copied from a
        # session on a real NSS 5.2 virtual machine in Azure.
        self.name_servers = ["168.63.129.16"]
        self.service_address = ""
        self.service_gateway = ""
        self.configure_runs = 0
        self.configure_answers = []
        self.configure_exit_status = 0
        self.configure_extra_question = None
        self.configure_accepts_address = True
        self.configure_success_text = "Successfully Applied Changes"
        self.dump_config_shows_service_interface = True
        self.autostart = False
        self.autostart_runs = 0
        self.autostart_exit_status = 0
        # `nss update-now`, `nss start` and `nss status`. The lines they print are copied from a session
        # on a real NSS 5.2 virtual machine in Azure, where the image came without the NSS binaries.
        # The exit statuses, and what `status` prints when NSS is installed but stopped, are assumed.
        self.nss_installed = False
        self.nss_pid = None
        self.update_runs = 0
        self.update_exit_status = 0
        self.update_seconds = 0
        self.start_runs = 0
        self.start_works = True
        self.nss_ends_after_start = False
        self.commands = []
        self.installs = 0
        self.dump_configs = 0
        self._sudo_cached = False
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(5)
        self.port = self._listener.getsockname()[1]
        self._transports = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                connection, _ = self._listener.accept()
            except OSError:
                return
            transport = paramiko.Transport(connection)
            transport.add_server_key(HOST_KEY)
            transport.set_subsystem_handler("sftp", paramiko.SFTPServer, FakeSftp)
            self._sudo_cached = False
            try:
                transport.start_server(server=FakeSshServer(self))
            except Exception:
                continue
            self._transports.append(transport)

    def stop(self):
        self._listener.close()
        for transport in self._transports:
            transport.close()

    def local_path(self, remote_path):
        assert remote_path.startswith("/home/zsroot/"), remote_path
        return str(self.home / remote_path[len("/home/zsroot/") :])

    def execute(self, channel, command, has_pty):
        # Let the server confirm the exec request before any output or the exit status is sent.
        time.sleep(0.05)
        self.commands.append(command)
        try:
            try:
                status = self._execute(channel, command, has_pty)
            except (OSError, EOFError):
                raise
            except Exception as error:  # a bug in the fake must fail the test, not hang it
                channel.sendall(f"fake vm error: {error!r}\n".encode())
                status = 99
            channel.send_exit_status(status)
            channel.close()
        except (OSError, EOFError):
            pass  # the client went away

    def shell(self, channel):
        """A login shell: it greets, shows a prompt, and the terminal echoes what is typed."""
        try:
            if self.login_shell == "silent":
                return
            time.sleep(0.05)
            channel.sendall(b"Last login: Wed Nov 11 18:00:00 2026\r\nZscaler NSS\r\n[zsroot@nss ~]$ ")
            while True:
                line = self._read_line(channel)
                if not line:
                    return
                self.typed.append(line)
                channel.sendall(line.encode() + b"\r\n")
                match = re.fullmatch(r'echo "([A-Z-]+=)\$PATH"', line)
                if match:
                    channel.sendall(f"{match.group(1)}{self.login_path}\r\n".encode())
                channel.sendall(b"[zsroot@nss ~]$ ")
        except (OSError, EOFError):
            pass  # the client went away

    def _which_nss(self, channel, path):
        if self.nss_directory not in path.split(":"):
            return 1
        channel.sendall(f"{self.nss_directory}/nss\n".encode())
        return 0

    def _execute(self, channel, command, has_pty):
        if command == "sh -c 'command -v nss'":
            return self._which_nss(channel, self.session_path)

        if command == "sh -c 'echo $PATH'":
            channel.sendall(f"{self.session_path}\n".encode())
            return 0

        match = re.fullmatch(r"env PATH='([^']+)' sh -c 'command -v nss'", command)
        if match:
            return self._which_nss(channel, match.group(1))

        match = re.fullmatch(
            r"sh -c 'for f in ([^;]+); do if \[ -x \$f \]; then echo \$f; exit 0; fi; done; exit 1'", command
        )
        if match:
            for candidate in match.group(1).split():
                if candidate == f"{self.nss_directory}/nss":
                    channel.sendall(f"{candidate}\n".encode())
                    return 0
            return 1

        match = re.fullmatch(r"cat > (\S+) && chmod 600 (\S+)", command)
        if match:
            data = bytearray()
            while True:
                chunk = channel.recv(65536)
                if not chunk:
                    break
                data.extend(chunk)
            Path(self.local_path(match.group(1))).write_bytes(bytes(data))
            return 0

        match = re.fullmatch(r"wc -c < (\S+)", command)
        if match:
            channel.sendall(f"    {os.path.getsize(self.local_path(match.group(1)))}\n".encode())
            return 0

        match = re.fullmatch(r"cat (\S+)", command)
        if match:
            path = Path(self.local_path(match.group(1)))
            if not path.exists():
                channel.sendall_stderr(f"cat: {match.group(1)}: No such file or directory\n".encode())
                return 1
            channel.sendall(path.read_bytes())
            return 0

        match = re.fullmatch(r"echo '([0-9a-f ./]+)' > (\S+)", command)
        if match:
            Path(self.local_path(match.group(2))).write_text(match.group(1) + "\n")
            return 0

        match = re.fullmatch(
            r"(?:cd /home/zsroot && )?(?:env PATH='(?P<path>[^']+)' )?sudo -p '(?P<prompt>[^']+)' (?P<nss>\S+) "
            r"(?P<action>install-cert NssCertificate\.zip|dump-config|configure|enable-autostart|"
            r"status|update-now|start)",
            command,
        )
        if match:
            if not has_pty:
                channel.sendall(b"sudo: sorry, you must have a tty to run sudo\n")
                return 1
            # sudo looks for a command without a directory in the PATH it was started with.
            search_path = (match.group("path") or self.session_path).split(":")
            found = match.group("nss") == f"{self.nss_directory}/nss" or (
                match.group("nss") == "nss" and self.nss_directory in search_path
            )
            if not found:
                channel.sendall(f"sudo: {match.group('nss')}: command not found\r\n".encode())
                return 1
            if self.sudo_needs_password and not self._sudo_cached:
                for _ in range(3):
                    channel.sendall(match.group("prompt").encode())
                    if self._read_line(channel) == self.sudo_password:
                        self._sudo_cached = True
                        break
                    channel.sendall(b"\r\nSorry, try again.\r\n")
                else:
                    channel.sendall(b"sudo: 3 incorrect password attempts\r\n")
                    return 1
            if match.group("action") == "dump-config":
                self.dump_configs += 1
                channel.sendall(b"Configured Values:\r\n\tCloudName:example\r\n\tnameserver:192.0.2.53\r\n")
                if self.service_address and self.dump_config_shows_service_interface:
                    channel.sendall(
                        f"\tsmnet_dev:{self.service_address}\r\n\tsmnet_dflt_gw:{self.service_gateway}\r\n".encode()
                    )
                return 0
            if match.group("action") == "configure":
                return self._configure(channel)
            if match.group("action") == "enable-autostart":
                self.autostart_runs += 1
                if self.autostart_exit_status == 0:
                    self.autostart = True
                    channel.sendall(b"Detected an Azure VM!!\r\nAuto-start of NSS enabled\r\n")
                return self.autostart_exit_status
            if match.group("action") == "update-now":
                self.update_runs += 1
                channel.sendall(b"Connecting to server...\r\n")
                time.sleep(self.update_seconds)
                if self.update_exit_status == 0:
                    self.nss_installed = True
                    channel.sendall(
                        b"Downloading latest version\r\nInstalling build /sc/smcdsc/nss_upgrade.sh\r\n"
                        b"Finished installation!\r\n"
                    )
                return self.update_exit_status
            if match.group("action") == "start":
                self.start_runs += 1
                channel.sendall(b"Detected an Azure VM!!\r\n")
                if not self.nss_installed:
                    channel.sendall(b"NSS service not installed\r\n")
                    return 1
                if not self.start_works:
                    channel.sendall(b"valid client cert present(old)\r\nNSS service failed to start\r\n")
                    return 1
                channel.sendall(b"valid client cert present(old)\r\nNSS service running with pid 2277\r\n")
                self.nss_pid = None if self.nss_ends_after_start else 2277
                return 0
            if match.group("action") == "status":
                channel.sendall(b"Detected an Azure VM!!\r\n")
                if not self.nss_installed:
                    channel.sendall(b"NSS service not installed\r\n")
                    return 1
                if self.nss_pid is None:
                    channel.sendall(b"valid client cert present(old)\r\nNSS service not running\r\n")
                    return 1
                channel.sendall(
                    f"valid client cert present(old)\r\nNSS service running with pid {self.nss_pid}\r\n".encode()
                )
                return 0
            self.installs += 1
            if not command.startswith("cd /home/zsroot && "):
                channel.sendall(b"NssCertificate.zip: file not found\r\n")
                return 1
            if not zipfile.is_zipfile(self.local_path("/home/zsroot/NssCertificate.zip")):
                channel.sendall(b"Invalid certificate file\r\n")
                return 1
            channel.sendall(b"Certificate installation in progress\r\n")
            return self.install_exit_status

        channel.sendall(f"unknown command: {command}\n".encode())
        return 127

    def _ask(self, channel, question):
        """Prints a question without a line end and reads the answer, which the terminal echoes."""
        channel.sendall(question.encode())
        answer = self._read_line(channel)
        channel.sendall(answer.encode() + b"\r\n")
        self.configure_answers.append(answer)
        return answer

    def _configure(self, channel):
        """`nss configure`, question by question, as the NSS image asks them."""
        self.configure_runs += 1
        channel.sendall(b"Detected an Azure VM!!\r\n")
        for name_server in self.name_servers:
            answer = self._ask(channel, f"nameserver:{name_server} (Options <c:change, d:delete, n:no change>) [n]")
            if answer not in ("", "n"):
                channel.sendall(b"This fake only keeps name servers.\r\n")
                return 2
        if self._ask(channel, "Do you wish to add a new nameserver? <n:no y:yes> [n]: ") not in ("", "n"):
            channel.sendall(b"This fake only keeps name servers.\r\n")
            return 2
        if self.configure_extra_question:
            self._ask(channel, self.configure_extra_question)
        while True:
            address = self._ask(
                channel, f"smnet_dev (Service interface IP address with netmask) [{self.service_address}]: "
            )
            if "/" in address and self.configure_accepts_address:
                break
            channel.sendall(b"Please re-enter netmask after IP address: (Ex.1.2.3.4/24)\r\n")
        gateway = self._ask(
            channel, f"smnet_dflt_gw (Service interface default gateway IP address) [{self.service_gateway}]: "
        )
        if self.configure_exit_status == 0:
            self.service_address, self.service_gateway = address, gateway
            channel.sendall(f"{self.configure_success_text}\r\n".encode())
        else:
            channel.sendall(b"Failed to apply changes\r\n")
        return self.configure_exit_status

    @staticmethod
    def _read_line(channel):
        line = bytearray()
        while not line.endswith(b"\n"):
            chunk = channel.recv(1)
            if not chunk:
                break
            line.extend(chunk)
        return line.decode().rstrip("\n")


@pytest.fixture(autouse=True)
def no_wait_after_nss_start(monkeypatch):
    monkeypatch.setattr(automation, "START_SETTLE_SECONDS", 0)


@pytest.fixture
def fake_vm(tmp_path):
    vm = FakeNssVm(tmp_path)
    yield vm
    vm.stop()


# --------------------------------------------------------------------------------------
# Fake ZIA client
# --------------------------------------------------------------------------------------


class FakeZia:
    """Stands in for the zscaler-sdk-python client: same call shapes, state kept in memory."""

    def __init__(self, servers=None, certificate=None):
        self.servers = list(servers or [])
        self.certificate = certificate_zip() if certificate is None else certificate
        self.created = []
        self.closed = 0
        self.zia = Mock()
        self.zia.nss_servers.list_nss_servers.side_effect = lambda: (list(self.servers), Mock(), None)
        self.zia.nss_servers.add_nss_server.side_effect = self._add
        self.zia.nss_servers.nss_download_certificate.side_effect = lambda nss_id: (self.certificate, Mock(), None)

    def _add(self, **kwargs):
        server = {"id": 9001, **kwargs}
        self.created.append(kwargs)
        self.servers.append(server)
        return (server, Mock(), None)

    def __exit__(self, *args):
        self.closed += 1


def settings_for(vm, **overrides):
    env = {
        "NSS_VM_HOST": "127.0.0.1",
        "NSS_VM_PORT": str(vm.port),
        "NSS_VM_PASSWORD": VM_PASSWORD,
        "NSS_SERVER_NAME": "Zscaler-NSS-VM",
        "NSS_SSH_WAIT_SECONDS": "5",
        "NSS_COMMAND_TIMEOUT_SECONDS": "20",
        "ZSCALER_CLIENT_ID": NOT_A_CLIENT_ID,
        "ZSCALER_CLIENT_SECRET": NOT_A_SECRET,
        "ZSCALER_VANITY_DOMAIN": "acme",
    }
    env.update(overrides)
    return automation.read_settings(env)


def run(vm, zia, **overrides):
    factory = Mock(return_value=zia)
    outputs = automation.run(settings_for(vm, **overrides), zia_client_factory=factory)
    return outputs, factory


# --------------------------------------------------------------------------------------
# Workflow
# --------------------------------------------------------------------------------------


def test_creates_the_server_and_installs_the_certificate(fake_vm, capsys):
    zia = FakeZia()

    outputs, _ = run(fake_vm, zia)

    assert outputs == {
        "nssServerId": "9001",
        "nssServerName": "Zscaler-NSS-VM",
        "nssServerCreated": True,
        "certificateInstalled": True,
        "serviceInterfaceConfigured": False,
        "nssRunning": False,
        "autostartEnabled": False,
    }
    assert zia.created == [{"name": "Zscaler-NSS-VM", "status": "ENABLED", "type": "NSS_FOR_WEB"}]
    assert (fake_vm.home / "NssCertificate.zip").read_bytes() == zia.certificate
    assert fake_vm.installs == 1 and fake_vm.dump_configs == 1
    assert (fake_vm.home / ".nss-certificate-installed").read_text().startswith("9001 ")

    printed = capsys.readouterr()
    assert VM_PASSWORD not in printed.out + printed.err
    assert NOT_A_SECRET not in printed.out + printed.err
    assert "NSS-CERTIFICATE-SUDO-PASSWORD" not in printed.out
    assert "Certificate installation in progress" in printed.out
    assert "CloudName:example" in printed.out


def test_running_again_changes_nothing(fake_vm):
    zia = FakeZia()
    run(fake_vm, zia)

    outputs, _ = run(fake_vm, zia)

    assert outputs["nssServerCreated"] is False
    assert outputs["certificateInstalled"] is False
    assert len(zia.created) == 1
    assert fake_vm.installs == 1


def test_running_again_with_a_rebuilt_archive_of_the_same_certificate_changes_nothing(fake_vm):
    zia = FakeZia()
    run(fake_vm, zia)
    first = zia.certificate
    zia.certificate = certificate_zip()
    assert automation.certificate_digest(first) == automation.certificate_digest(zia.certificate)

    outputs, _ = run(fake_vm, zia)

    assert outputs["certificateInstalled"] is False


def test_a_regenerated_certificate_is_installed_again(fake_vm):
    zia = FakeZia()
    run(fake_vm, zia)
    zia.certificate = certificate_zip("certificate-2")

    outputs, _ = run(fake_vm, zia)

    assert outputs["certificateInstalled"] is True
    assert fake_vm.installs == 2
    assert (fake_vm.home / "NssCertificate.zip").read_bytes() == zia.certificate


def test_force_installs_again(fake_vm):
    zia = FakeZia()
    run(fake_vm, zia)

    outputs, _ = run(fake_vm, zia, NSS_FORCE_CERTIFICATE_INSTALL="true")

    assert outputs["certificateInstalled"] is True
    assert fake_vm.installs == 2


def test_an_existing_server_is_reused_and_not_changed(fake_vm):
    zia = FakeZia(servers=[{"id": 77, "name": "Zscaler-NSS-VM", "state": "UNHEALTHY"}, {"id": 78, "name": "other"}])

    outputs, _ = run(fake_vm, zia)

    assert outputs["nssServerId"] == "77" and outputs["nssServerCreated"] is False
    assert zia.created == []
    zia.zia.nss_servers.nss_download_certificate.assert_called_once_with("77")
    assert fake_vm.installs == 1


def test_a_healthy_existing_server_is_refused(fake_vm):
    zia = FakeZia(servers=[{"id": 77, "name": "Zscaler-NSS-VM", "state": "HEALTHY"}])

    with pytest.raises(automation.AutomationError, match="only one active NSS virtual machine"):
        run(fake_vm, zia)

    assert fake_vm.installs == 0
    assert not (fake_vm.home / "NssCertificate.zip").exists()


def test_a_healthy_existing_server_can_be_allowed(fake_vm):
    zia = FakeZia(servers=[{"id": 77, "name": "Zscaler-NSS-VM", "state": "HEALTHY"}])

    outputs, _ = run(fake_vm, zia, NSS_ALLOW_HEALTHY_SERVER="true")

    assert outputs["certificateInstalled"] is True


def test_a_healthy_server_whose_certificate_is_on_this_machine_is_left_alone(fake_vm):
    zia = FakeZia(servers=[{"id": 77, "name": "Zscaler-NSS-VM", "state": "UNHEALTHY"}])
    run(fake_vm, zia)
    zia.servers[0]["state"] = "HEALTHY"

    outputs, _ = run(fake_vm, zia)

    assert outputs["certificateInstalled"] is False


def test_duplicate_server_names_are_refused(fake_vm):
    zia = FakeZia(servers=[{"id": 1, "name": "Zscaler-NSS-VM"}, {"id": 2, "name": "Zscaler-NSS-VM"}])

    with pytest.raises(automation.AutomationError, match="2 NSS servers named"):
        run(fake_vm, zia)


def test_a_download_that_is_not_a_zip_archive_is_not_copied(fake_vm):
    zia = FakeZia(certificate=b'{"code":"RESOURCE_NOT_FOUND"}')

    with pytest.raises(automation.AutomationError, match="not a ZIP archive"):
        run(fake_vm, zia)

    assert list(fake_vm.home.iterdir()) == []


def test_zia_errors_stop_the_run(fake_vm):
    zia = FakeZia()
    zia.zia.nss_servers.add_nss_server.side_effect = lambda **kwargs: (None, Mock(), Exception("DUPLICATE_ITEM"))

    with pytest.raises(automation.AutomationError, match="DUPLICATE_ITEM"):
        run(fake_vm, zia)

    assert fake_vm.installs == 0


def test_a_failed_install_is_reported_and_not_marked_as_installed(fake_vm, capsys):
    fake_vm.install_exit_status = 3

    with pytest.raises(
        automation.AutomationError, match=r"exit status 3\. Last output: Certificate installation in progress$"
    ):
        run(fake_vm, FakeZia())

    assert not (fake_vm.home / ".nss-certificate-installed").exists()
    assert fake_vm.dump_configs == 0
    assert "Certificate installation in progress" in capsys.readouterr().out


def test_dump_config_output_can_be_kept_out_of_the_log(fake_vm, capsys):
    run(fake_vm, FakeZia(), NSS_PRINT_DUMP_CONFIG="false")

    printed = capsys.readouterr().out
    assert "CloudName" not in printed
    assert "'sudo nss dump-config' succeeded (3 lines)" in printed


# --------------------------------------------------------------------------------------
# Nothing activates the ZIA configuration
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("existing", [False, True], ids=["new-server", "existing-server"])
def test_the_configuration_is_not_activated_and_the_session_is_not_closed(fake_vm, existing):
    zia = FakeZia(servers=[{"id": 77, "name": "Zscaler-NSS-VM"}] if existing else [])

    outputs, _ = run(fake_vm, zia)

    assert outputs["certificateInstalled"] is True
    assert zia.zia.activate.mock_calls == []
    assert zia.closed == 0


def test_the_session_is_not_closed_when_the_run_fails(fake_vm):
    zia = FakeZia(certificate=b"not a zip archive")

    with pytest.raises(automation.AutomationError):
        run(fake_vm, zia)

    assert zia.created != []
    assert zia.zia.activate.mock_calls == []
    assert zia.closed == 0


def test_the_script_has_no_activation_setting_or_call():
    source = SCRIPT.read_text(encoding="utf-8")

    assert "NSS_ACTIVATE_ZIA_CHANGES" not in source
    assert ".activate" not in source
    assert "__exit__(" not in source and "deauthenticate(" not in source
    assert "activate" not in automation.read_settings({**BASE_ENV, **ONEAPI_ENV})


@pytest.mark.parametrize("server_type", SUPPORTED_SERVER_TYPES)
def test_every_supported_server_type_is_passed_to_zia(fake_vm, server_type):
    zia = FakeZia()

    run(fake_vm, zia, NSS_SERVER_TYPE=server_type)

    assert zia.created == [{"name": "Zscaler-NSS-VM", "status": "ENABLED", "type": server_type}]


# --------------------------------------------------------------------------------------
# SSH
# --------------------------------------------------------------------------------------


# --------------------------------------------------------------------------------------
# Finding the nss command
# --------------------------------------------------------------------------------------

ASK_FOR_THE_LOGIN_PATH = 'echo "NSS-CERTIFICATE-LOGIN-PATH=$PATH"'


def nss_commands(vm):
    return [command for command in vm.commands if re.search(r"(install-cert|dump-config)", command)]


def test_the_fake_vm_fails_like_the_nss_image_when_nss_is_run_without_its_directory(fake_vm):
    """What the first real run met: sshd starts the command with a PATH that has no nss."""
    vm = automation.NssVm(settings_for(fake_vm))
    vm.connect()
    try:
        status, output = vm.run("cd /home/zsroot && sudo -p 'password:' nss install-cert NssCertificate.zip", sudo=True)
    finally:
        vm.close()

    assert (status, output) == (1, "sudo: nss: command not found")


def test_nss_is_found_with_the_path_of_a_login_shell(fake_vm, capsys):
    outputs, _ = run(fake_vm, FakeZia())

    assert outputs["certificateInstalled"] is True
    assert fake_vm.typed == [ASK_FOR_THE_LOGIN_PATH]
    path = f"{SESSION_PATH}:/sc/bin"
    assert nss_commands(fake_vm) == [
        f"cd /home/zsroot && env PATH='{path}' sudo -p 'NSS-CERTIFICATE-SUDO-PASSWORD:' /sc/bin/nss "
        "install-cert NssCertificate.zip",
        f"env PATH='{path}' sudo -p 'NSS-CERTIFICATE-SUDO-PASSWORD:' /sc/bin/nss dump-config",
    ]
    assert "The nss command is /sc/bin/nss, found with the PATH of a login shell." in capsys.readouterr().out


def test_nss_in_the_path_of_the_session_needs_no_login_shell(fake_vm, capsys):
    fake_vm.session_path = f"{SESSION_PATH}:/sc/bin"

    outputs, _ = run(fake_vm, FakeZia())

    assert outputs["certificateInstalled"] is True
    assert fake_vm.typed == []
    assert nss_commands(fake_vm) == [
        "cd /home/zsroot && sudo -p 'NSS-CERTIFICATE-SUDO-PASSWORD:' /sc/bin/nss install-cert NssCertificate.zip",
        "sudo -p 'NSS-CERTIFICATE-SUDO-PASSWORD:' /sc/bin/nss dump-config",
    ]
    assert "The nss command is /sc/bin/nss.\n" in capsys.readouterr().out


@pytest.mark.parametrize(
    "login_shell, login_path",
    [(False, None), ("silent", None), (True, "/usr/bin:/bin"), (True, "/usr/bin:/bin; rm -rf /")],
    ids=["no shell", "a shell that says nothing", "a login PATH without nss", "a login PATH that is not a PATH"],
)
def test_nss_is_found_in_a_known_directory(fake_vm, capsys, monkeypatch, login_shell, login_path):
    monkeypatch.setattr(automation, "LOGIN_SHELL_SECONDS", 3)
    fake_vm.login_shell = login_shell
    fake_vm.login_path = login_path or fake_vm.login_path
    fake_vm.nss_directory = "/opt/zscaler/bin"

    outputs, _ = run(fake_vm, FakeZia())

    assert outputs["certificateInstalled"] is True
    path = "/opt/zscaler/bin:" + ("/usr/bin:/bin" if login_path == "/usr/bin:/bin" else SESSION_PATH)
    assert nss_commands(fake_vm)[0] == (
        f"cd /home/zsroot && env PATH='{path}' sudo -p 'NSS-CERTIFICATE-SUDO-PASSWORD:' /opt/zscaler/bin/nss "
        "install-cert NssCertificate.zip"
    )
    assert "No PATH has it; it was found by looking in /opt/zscaler/bin." in capsys.readouterr().out


def test_a_vm_without_the_nss_command_stops_before_zia_is_contacted(fake_vm):
    fake_vm.nss_directory = "/somewhere/else"
    fake_vm.login_path = "/usr/bin:/bin"
    factory = Mock()

    with pytest.raises(automation.AutomationError) as raised:
        automation.run(settings_for(fake_vm), zia_client_factory=factory)

    factory.assert_not_called()
    assert str(raised.value) == (
        "The nss command was not found on the virtual machine. "
        f"PATH of the SSH session: {SESSION_PATH}. "
        "PATH of a login shell: /usr/bin:/bin. "
        "Also looked in: /sc/bin, /sc/sbin, /usr/local/bin, /usr/local/sbin, /opt/zscaler/bin. "
        "Nothing was created or changed in ZIA."
    )
    assert not (fake_vm.home / "NssCertificate.zip").exists()


def test_nss_is_looked_for_before_the_marker_is_read_and_before_anything_is_copied(fake_vm):
    run(fake_vm, FakeZia())

    assert fake_vm.commands[0] == "sh -c 'command -v nss'"
    first_other = next(
        i for i, command in enumerate(fake_vm.commands) if "nss" not in command and "PATH" not in command
    )
    assert all("command -v nss" not in command for command in fake_vm.commands[first_other:])


# --------------------------------------------------------------------------------------
# nss configure, nss update-now, nss start and nss enable-autostart
# --------------------------------------------------------------------------------------

SERVICE_INTERFACE = {
    "NSS_SERVICE_INTERFACE_ADDRESS": "192.168.100.4/25",
    "NSS_SERVICE_INTERFACE_GATEWAY": "192.168.100.1",
}
CONFIGURE_MARKER = ".nss-service-interface-configured"


def actions(vm):
    """The nss commands in the order they ran."""
    found = [
        re.search(r"/nss (install-cert|dump-config|configure|enable-autostart|status|update-now|start)\b", command)
        for command in vm.commands
    ]
    return [match.group(1) for match in found if match]


def test_the_service_interface_is_configured_before_the_certificate_is_installed(fake_vm, capsys):
    outputs, _ = run(
        fake_vm, FakeZia(), NSS_ENABLE_AUTOSTART="true", NSS_PRINT_DUMP_CONFIG="false", **SERVICE_INTERFACE
    )

    assert outputs["serviceInterfaceConfigured"] is True and outputs["certificateInstalled"] is True
    assert outputs["autostartEnabled"] is True
    # One answer for each question, in the order of the questions; the name server is kept.
    assert fake_vm.configure_answers == ["n", "n", "192.168.100.4/25", "192.168.100.1"]
    assert (fake_vm.service_address, fake_vm.service_gateway) == ("192.168.100.4/25", "192.168.100.1")
    assert fake_vm.name_servers == ["168.63.129.16"]
    assert fake_vm.autostart is True
    assert actions(fake_vm) == [
        "dump-config",
        "configure",
        "dump-config",
        "install-cert",
        "dump-config",
        "enable-autostart",
    ]
    assert (fake_vm.home / CONFIGURE_MARKER).read_text().strip() == "192.168.100.4/25 192.168.100.1"

    printed = capsys.readouterr().out
    assert "==> Successfully Applied Changes" in printed
    assert "==> Auto-start of NSS enabled" in printed
    assert "'sudo nss dump-config' shows the new values." in printed
    assert VM_PASSWORD not in printed and "NSS-CERTIFICATE-SUDO-PASSWORD" not in printed
    # The check of the configuration is not written to the log.
    assert "CloudName" not in printed


def test_the_service_interface_is_configured_before_zia_is_contacted(fake_vm):
    fake_vm.configure_exit_status = 4
    factory = Mock()

    with pytest.raises(
        automation.AutomationError, match=r"'sudo nss configure' failed with exit status 4\. Last output: .*Failed"
    ):
        automation.run(settings_for(fake_vm, **SERVICE_INTERFACE), zia_client_factory=factory)

    factory.assert_not_called()
    assert not (fake_vm.home / CONFIGURE_MARKER).exists()
    assert fake_vm.installs == 0


def test_every_name_server_is_kept(fake_vm):
    fake_vm.name_servers = ["168.63.129.16", "192.0.2.53", "192.0.2.54"]

    run(fake_vm, FakeZia(), **SERVICE_INTERFACE)

    assert fake_vm.configure_answers == ["n", "n", "n", "n", "192.168.100.4/25", "192.168.100.1"]


def test_running_again_does_not_configure_again(fake_vm, capsys):
    zia = FakeZia()
    run(fake_vm, zia, NSS_ENABLE_AUTOSTART="true", **SERVICE_INTERFACE)
    capsys.readouterr()

    outputs, _ = run(fake_vm, zia, NSS_ENABLE_AUTOSTART="true", **SERVICE_INTERFACE)

    assert outputs["serviceInterfaceConfigured"] is False and outputs["certificateInstalled"] is False
    assert fake_vm.configure_runs == 1 and fake_vm.installs == 1
    assert "by an earlier run. Nothing to do." in capsys.readouterr().out
    # The automatic start is cheap to enable again, and is enabled on every run.
    assert fake_vm.autostart_runs == 2


def test_a_service_interface_that_already_has_the_values_is_left_alone(fake_vm, capsys):
    """Configured by hand, so there is no marker file: dump-config is what tells."""
    fake_vm.service_address, fake_vm.service_gateway = "192.168.100.4/25", "192.168.100.1"

    outputs, _ = run(fake_vm, FakeZia(), **SERVICE_INTERFACE)

    assert outputs["serviceInterfaceConfigured"] is False
    assert fake_vm.configure_runs == 0
    assert "already has 192.168.100.4/25 and gateway 192.168.100.1" in capsys.readouterr().out
    assert (fake_vm.home / CONFIGURE_MARKER).exists()


def test_other_values_on_the_vm_are_replaced(fake_vm):
    """The address Azure gave the network interface is the only one that works."""
    fake_vm.service_address, fake_vm.service_gateway = "10.10.10.1/24", "10.10.10.10"

    outputs, _ = run(fake_vm, FakeZia(), **SERVICE_INTERFACE)

    assert outputs["serviceInterfaceConfigured"] is True
    assert (fake_vm.service_address, fake_vm.service_gateway) == ("192.168.100.4/25", "192.168.100.1")


def test_new_values_are_configured_although_an_earlier_run_left_a_marker(fake_vm):
    zia = FakeZia()
    run(fake_vm, zia, **SERVICE_INTERFACE)

    outputs, _ = run(
        fake_vm, zia, NSS_SERVICE_INTERFACE_ADDRESS="192.168.100.9/25", NSS_SERVICE_INTERFACE_GATEWAY="192.168.100.1"
    )

    assert outputs["serviceInterfaceConfigured"] is True
    assert fake_vm.service_address == "192.168.100.9/25"
    assert (fake_vm.home / CONFIGURE_MARKER).read_text().strip() == "192.168.100.9/25 192.168.100.1"


def test_a_question_the_script_does_not_know_stops_the_run_and_is_not_answered(fake_vm, monkeypatch):
    monkeypatch.setattr(automation, "QUESTION_WAIT_SECONDS", 1)
    fake_vm.configure_extra_question = "Enable the new option? <n:no y:yes> [y]: "
    factory = Mock()

    with pytest.raises(automation.AutomationError) as raised:
        automation.run(settings_for(fake_vm, **SERVICE_INTERFACE), zia_client_factory=factory)

    message = str(raised.value)
    assert "waits for input after 'Enable the new option? <n:no y:yes> [y]:'" in message
    assert "not a question this script knows" in message
    # Only the two name server questions were answered, and nothing was configured or created.
    assert fake_vm.configure_answers[:2] == ["n", "n"]
    assert all(answer == "" for answer in fake_vm.configure_answers[2:])
    assert fake_vm.service_address == ""
    factory.assert_not_called()
    assert not (fake_vm.home / CONFIGURE_MARKER).exists()


def test_an_address_the_vm_does_not_accept_is_not_typed_again(fake_vm):
    fake_vm.configure_accepts_address = False

    with pytest.raises(automation.AutomationError) as raised:
        run(fake_vm, FakeZia(), **SERVICE_INTERFACE)

    message = str(raised.value)
    assert "did not accept the answer '192.168.100.4/25'" in message
    assert "Please re-enter netmask after IP address" in message
    assert fake_vm.configure_answers.count("192.168.100.4/25") == 1


def test_configure_that_reports_success_but_kept_other_values_is_an_error(fake_vm, monkeypatch):
    original = fake_vm._configure

    def configure_and_forget(channel):
        status = original(channel)
        fake_vm.service_address = "192.168.100.77/25"
        return status

    monkeypatch.setattr(fake_vm, "_configure", configure_and_forget)

    with pytest.raises(automation.AutomationError, match=r"shows the service interface as 192\.168\.100\.77/25"):
        run(fake_vm, FakeZia(), **SERVICE_INTERFACE)

    assert not (fake_vm.home / CONFIGURE_MARKER).exists()


def test_the_success_line_counts_when_dump_config_does_not_show_the_interface(fake_vm, capsys):
    fake_vm.dump_config_shows_service_interface = False

    outputs, _ = run(fake_vm, FakeZia(), **SERVICE_INTERFACE)

    assert outputs["serviceInterfaceConfigured"] is True
    assert "'Successfully Applied Changes' from nss configure is taken as the result." in capsys.readouterr().out


def test_neither_the_success_line_nor_dump_config_is_an_error(fake_vm):
    fake_vm.dump_config_shows_service_interface = False
    fake_vm.configure_success_text = "Done"

    with pytest.raises(automation.AutomationError, match="ended without 'Successfully Applied Changes'"):
        run(fake_vm, FakeZia(), **SERVICE_INTERFACE)


def test_without_the_service_interface_settings_nothing_is_configured(fake_vm, capsys):
    outputs, _ = run(fake_vm, FakeZia())

    assert outputs["serviceInterfaceConfigured"] is False and outputs["autostartEnabled"] is False
    assert actions(fake_vm) == ["install-cert", "dump-config"]
    assert "'nss configure' is left to you" in capsys.readouterr().out


def test_a_failed_enable_autostart_fails_the_run(fake_vm):
    fake_vm.autostart_exit_status = 1

    with pytest.raises(automation.AutomationError, match="'sudo nss enable-autostart' failed with exit status 1"):
        run(fake_vm, FakeZia(), NSS_ENABLE_AUTOSTART="true")


START = {"NSS_START": "true", "NSS_ENABLE_AUTOSTART": "true"}


def test_nss_is_updated_and_started_after_the_certificate_as_in_the_zscaler_guide(fake_vm, capsys):
    outputs, _ = run(fake_vm, FakeZia(), **START, **SERVICE_INTERFACE)

    assert outputs["nssRunning"] is True and outputs["autostartEnabled"] is True
    assert fake_vm.nss_pid == 2277 and fake_vm.autostart is True
    assert actions(fake_vm) == [
        "dump-config",
        "configure",
        "dump-config",
        "install-cert",
        "dump-config",
        "status",
        "update-now",
        "start",
        "status",
        "enable-autostart",
    ]

    printed = capsys.readouterr().out
    assert "==> Finished installation!" in printed
    assert "==> NSS service running with pid 2277" in printed
    assert "==> NSS is running." in printed
    assert "WARNING" not in printed
    assert VM_PASSWORD not in printed and "NSS-CERTIFICATE-SUDO-PASSWORD" not in printed


def test_without_nss_start_nothing_is_updated_or_started(fake_vm, capsys):
    outputs, _ = run(fake_vm, FakeZia(), NSS_ENABLE_AUTOSTART="true", **SERVICE_INTERFACE)

    assert outputs["nssRunning"] is False
    assert not [action for action in actions(fake_vm) if action in ("status", "update-now", "start")]
    assert "NSS_START is not set, so NSS is not started." in capsys.readouterr().out


def test_nss_that_already_runs_is_neither_updated_nor_started_again(fake_vm, capsys):
    zia = FakeZia()
    run(fake_vm, zia, **START)
    capsys.readouterr()

    outputs, _ = run(fake_vm, zia, **START)

    assert outputs["nssRunning"] is True
    assert fake_vm.update_runs == 1 and fake_vm.start_runs == 1
    assert "NSS is already running. Nothing to update or start." in capsys.readouterr().out


def test_nss_that_was_stopped_is_started_again(fake_vm):
    zia = FakeZia()
    run(fake_vm, zia, **START)
    fake_vm.nss_pid = None

    outputs, _ = run(fake_vm, zia, **START)

    assert outputs["nssRunning"] is True and fake_vm.nss_pid == 2277
    assert fake_vm.start_runs == 2


def test_an_update_that_fails_ends_the_run_with_a_warning_and_not_with_an_error(fake_vm, capsys):
    fake_vm.update_exit_status = 96

    outputs, _ = run(fake_vm, FakeZia(), **START)

    # The rest of the run stands: the certificate is installed and the automatic start is enabled.
    assert outputs["nssRunning"] is False
    assert outputs["certificateInstalled"] is True and outputs["nssServerCreated"] is True
    assert fake_vm.autostart is True
    assert fake_vm.start_runs == 1

    printed = capsys.readouterr()
    assert "==> WARNING: NSS is not running. 'sudo nss update-now' ended with exit status 96" in printed.out
    assert "the client certificate is invalid" in printed.out
    assert "'sudo nss status' does not show NSS running. Last output: " in printed.out
    assert "NSS service not installed" in printed.out
    assert "is a pending change until it is activated in ZIA" in printed.out
    assert "==> NSS is running." not in printed.out
    assert printed.err == ""


def test_the_warning_does_not_mention_activation_when_the_server_object_was_reused(fake_vm, capsys):
    fake_vm.update_exit_status = 97
    zia = FakeZia(servers=[{"id": 77, "name": "Zscaler-NSS-VM", "state": "UNHEALTHY"}])

    outputs, _ = run(fake_vm, zia, **START)

    assert outputs["nssRunning"] is False
    printed = capsys.readouterr().out
    assert "a timeout occurred while contacting the upgrade server" in printed
    assert "pending change" not in printed.split("WARNING", 1)[1]


def test_nss_status_decides_and_not_the_exit_status_of_the_update(fake_vm, capsys):
    """What update-now returns when there is nothing to update is not known."""
    fake_vm.nss_installed = True
    fake_vm.update_exit_status = 3

    outputs, _ = run(fake_vm, FakeZia(), **START)

    assert outputs["nssRunning"] is True
    printed = capsys.readouterr().out
    assert "'sudo nss update-now' ended with exit status 3. NSS is started all the same" in printed
    assert "WARNING" not in printed


def test_nss_that_does_not_start_ends_the_run_with_a_warning(fake_vm, capsys):
    fake_vm.start_works = False

    outputs, _ = run(fake_vm, FakeZia(), **START)

    assert outputs["nssRunning"] is False and fake_vm.autostart is True
    printed = capsys.readouterr().out
    assert "WARNING: NSS is not running. 'sudo nss start' ended with exit status 1" in printed
    assert "Last output: Detected an Azure VM!! / valid client cert present(old) / NSS service not running" in printed


def test_nss_that_ends_after_its_start_is_not_reported_as_running(fake_vm, capsys):
    """`nss start` said that NSS runs; `nss status`, asked in a new session, says that it does not."""
    fake_vm.nss_ends_after_start = True

    outputs, _ = run(fake_vm, FakeZia(), **START)

    assert outputs["nssRunning"] is False
    assert actions(fake_vm)[-4:] == ["update-now", "start", "status", "enable-autostart"]
    assert "WARNING: NSS is not running. 'sudo nss start' ended with exit status 0" in capsys.readouterr().out


def test_the_update_has_its_own_time_limit(fake_vm):
    fake_vm.update_seconds = 3

    outputs, _ = run(fake_vm, FakeZia(), NSS_COMMAND_TIMEOUT_SECONDS="2", NSS_UPDATE_TIMEOUT_SECONDS="20", **START)

    assert outputs["nssRunning"] is True


def test_an_update_that_takes_too_long_ends_the_run_with_a_warning(fake_vm, capsys):
    fake_vm.update_seconds = 3

    outputs, _ = run(fake_vm, FakeZia(), NSS_UPDATE_TIMEOUT_SECONDS="1", **START)

    assert outputs["nssRunning"] is False
    # NSS is not started after an update that may still be going on.
    assert fake_vm.start_runs == 0
    assert "WARNING: NSS is not running. A command on the virtual machine did not finish within 1 seconds." in (
        capsys.readouterr().out
    )


def test_the_time_limit_of_the_update_must_be_a_number():
    with pytest.raises(automation.AutomationError, match="NSS_UPDATE_TIMEOUT_SECONDS must be a positive whole number"):
        automation.read_settings({"NSS_UPDATE_TIMEOUT_SECONDS": "ten minutes"})


@pytest.mark.parametrize(
    "dump_config, expected",
    [
        ("Configured Values:\n\tsmnet_dev:192.168.100.4/25\n\tsmnet_dflt_gw:192.168.100.1\n", True),
        ("smnet_dev = 192.168.100.4/25\r\nsmnet_dflt_gw = 192.168.100.1\r\n", True),
        ("Configured Values:\n\tnameserver:168.63.129.16\n", False),
        ("smnet_dev:192.168.100.4/25\n", False),
        ("", False),
    ],
)
def test_reading_the_service_interface_from_dump_config(dump_config, expected):
    values = automation.configured_service_interface(dump_config)
    assert (values == ("192.168.100.4/25", "192.168.100.1")) is expected


@pytest.mark.parametrize(
    "address, gateway, expected",
    [
        ("192.168.100.4/25", "", "must be set together"),
        ("", "192.168.100.1", "must be set together"),
        ("192.168.100.4", "192.168.100.1", "with a prefix length"),
        ("192.168.100.4/33", "192.168.100.1", "with a prefix length"),
        ("192.168.100.4/25; reboot", "192.168.100.1", "with a prefix length"),
        ("fd00::4/64", "fd00::1", "with a prefix length"),
        ("192.168.100.4/25", "gateway", "must be an IPv4 address"),
        ("192.168.100.4/25", "192.168.100.1\ny", "must be an IPv4 address"),
        ("192.168.100.4/25", "192.168.100.129", "must be another address in the subnet"),
        ("192.168.100.4/25", "192.168.100.4", "must be another address in the subnet"),
    ],
)
def test_service_interface_settings_are_validated(address, gateway, expected):
    env = {**BASE_ENV, **ONEAPI_ENV, "NSS_SERVICE_INTERFACE_ADDRESS": address, "NSS_SERVICE_INTERFACE_GATEWAY": gateway}
    with pytest.raises(automation.AutomationError, match=expected):
        automation.read_settings(env)


# --------------------------------------------------------------------------------------
# The log
# --------------------------------------------------------------------------------------


def test_every_log_line_starts_with_the_mark(fake_vm, capsys):
    """Azure returns the log of the deployment script as one line; the mark shows where lines began."""
    run(fake_vm, FakeZia(), NSS_PRINT_DUMP_CONFIG="true")

    lines = capsys.readouterr().out.splitlines()
    assert len(lines) > 10
    assert [line for line in lines if not line.startswith("==> ")] == []
    # Lines that came from the virtual machine are marked one by one.
    assert "==> Configured Values:" in lines and "==> \tCloudName:example" in lines


def test_a_wrong_vm_password_stops_before_zia_is_contacted(fake_vm):
    factory = Mock()

    with pytest.raises(automation.AutomationError, match="rejected the password"):
        automation.run(settings_for(fake_vm, NSS_VM_PASSWORD=NOT_A_WRONG_VM_PASSWORD), zia_client_factory=factory)

    factory.assert_not_called()


def test_an_unreachable_vm_stops_before_zia_is_contacted(fake_vm, monkeypatch):
    monkeypatch.setattr(automation.time, "sleep", lambda seconds: None)
    port = fake_vm.port
    fake_vm.stop()
    factory = Mock()

    with pytest.raises(automation.AutomationError, match="did not answer within 1 seconds"):
        automation.run(
            settings_for(fake_vm, NSS_VM_PORT=str(port), NSS_SSH_WAIT_SECONDS="1"), zia_client_factory=factory
        )

    factory.assert_not_called()


@pytest.mark.parametrize(
    "auth_methods, working",
    [
        (("password",), None),
        (("keyboard-interactive",), None),
        (("password", "keyboard-interactive"), ("keyboard-interactive",)),
    ],
    ids=["password", "keyboard-interactive", "password-offered-but-only-interactive-works"],
)
def test_authentication_methods(tmp_path, auth_methods, working):
    vm = FakeNssVm(tmp_path, auth_methods=auth_methods, working_auth_methods=working)
    try:
        outputs, _ = run(vm, FakeZia())
    finally:
        vm.stop()

    assert outputs["certificateInstalled"] is True


def test_public_key_only_servers_are_reported(tmp_path):
    vm = FakeNssVm(tmp_path, auth_methods=("publickey",))
    try:
        with pytest.raises(automation.AutomationError, match="neither password nor keyboard-interactive"):
            run(vm, FakeZia())
    finally:
        vm.stop()


def test_the_certificate_is_copied_with_sftp_when_the_vm_offers_it(tmp_path):
    vm = FakeNssVm(tmp_path, sftp=True)
    try:
        zia = FakeZia()
        run(vm, zia)
    finally:
        vm.stop()

    assert vm.sftp_uploads == 1
    assert not [command for command in vm.commands if command.startswith("cat > ")]
    assert (tmp_path / "NssCertificate.zip").read_bytes() == zia.certificate
    assert (tmp_path / "NssCertificate.zip").stat().st_mode & 0o777 == 0o600


def test_the_certificate_is_copied_through_the_session_without_sftp(fake_vm):
    zia = FakeZia()

    run(fake_vm, zia)

    assert fake_vm.sftp_uploads == 0
    assert "cat > /home/zsroot/NssCertificate.zip && chmod 600 /home/zsroot/NssCertificate.zip" in fake_vm.commands


def test_sudo_without_a_password_prompt(fake_vm):
    fake_vm.sudo_needs_password = False

    outputs, _ = run(fake_vm, FakeZia())

    assert outputs["certificateInstalled"] is True


def test_a_password_that_sudo_rejects_is_reported_once(fake_vm, capsys):
    fake_vm.sudo_password = NOT_A_OTHER_SUDO_PASSWORD

    with pytest.raises(automation.AutomationError, match="sudo rejected the password"):
        run(fake_vm, FakeZia())

    assert fake_vm.installs == 0
    assert VM_PASSWORD not in capsys.readouterr().out


def test_the_host_key_fingerprint_is_checked_when_given(fake_vm):
    fingerprint = HOST_KEY.fingerprint

    outputs, _ = run(fake_vm, FakeZia(), NSS_SSH_HOST_KEY_FINGERPRINT=fingerprint)
    assert outputs["certificateInstalled"] is True

    factory = Mock()
    with pytest.raises(automation.AutomationError, match="not the fingerprint"):
        automation.run(
            settings_for(fake_vm, NSS_SSH_HOST_KEY_FINGERPRINT="SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
            zia_client_factory=factory,
        )
    factory.assert_not_called()


# --------------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------------

BASE_ENV = {"NSS_VM_HOST": "192.0.2.10", "NSS_VM_PASSWORD": NOT_A_SECRET, "NSS_SERVER_NAME": "nss"}
ONEAPI_ENV = {"ZSCALER_CLIENT_ID": NOT_A_CLIENT_ID, "ZSCALER_CLIENT_SECRET": NOT_A_SECRET, "ZSCALER_VANITY_DOMAIN": "acme"}
LEGACY_ENV = {
    "ZIA_USERNAME": "admin@example.com",
    "ZIA_PASSWORD": NOT_A_SECRET,
    "ZIA_API_KEY": NOT_A_SECRET,
    "ZIA_CLOUD": "zscaler",
}


def test_settings_defaults():
    settings = automation.read_settings({**BASE_ENV, **ONEAPI_ENV})

    assert settings["auth"] == "oneapi"
    assert settings["vm_username"] == "zsroot" and settings["vm_port"] == 22
    assert settings["server_type"] == "NSS_FOR_WEB"
    assert settings["allow_healthy_server"] is False
    assert settings["print_dump_config"] is True


def test_the_supported_server_types_are_exactly_the_documented_ones():
    assert automation.NSS_SERVER_TYPES == SUPPORTED_SERVER_TYPES
    for server_type in SUPPORTED_SERVER_TYPES:
        settings = automation.read_settings({**BASE_ENV, **ONEAPI_ENV, "NSS_SERVER_TYPE": server_type})
        assert settings["server_type"] == server_type


def test_settings_auth_selection():
    assert automation.read_settings({**BASE_ENV, **LEGACY_ENV})["auth"] == "legacy"
    assert automation.read_settings({**BASE_ENV, **LEGACY_ENV, **ONEAPI_ENV})["auth"] == "oneapi"
    private_key = {"ZSCALER_CLIENT_ID": NOT_A_CLIENT_ID, "ZSCALER_PRIVATE_KEY": NOT_A_SECRET, "ZSCALER_VANITY_DOMAIN": "acme"}
    assert automation.read_settings({**BASE_ENV, **private_key})["auth"] == "oneapi"


@pytest.mark.parametrize(
    "env, expected",
    [
        ({}, "NSS_VM_HOST; NSS_VM_PASSWORD; NSS_SERVER_NAME; Zscaler credentials"),
        (
            {**BASE_ENV, "ZSCALER_CLIENT_ID": NOT_A_CLIENT_ID},
            "ZSCALER_VANITY_DOMAIN; ZSCALER_CLIENT_SECRET or ZSCALER_PRIVATE_KEY",
        ),
        ({**BASE_ENV, "ZIA_USERNAME": "admin"}, "ZIA_PASSWORD; ZIA_API_KEY; ZIA_CLOUD"),
        ({**BASE_ENV, **ONEAPI_ENV, "NSS_VM_PASSWORD": ""}, "NSS_VM_PASSWORD"),
        ({**BASE_ENV, **ONEAPI_ENV, "NSS_SERVER_TYPE": "NONE"}, "NSS_SERVER_TYPE must be one of"),
        ({**BASE_ENV, **ONEAPI_ENV, "NSS_SERVER_TYPE": "nss_for_web"}, "NSS_SERVER_TYPE must be one of"),
        (
            {**BASE_ENV, **ONEAPI_ENV, "NSS_ALLOW_HEALTHY_SERVER": "maybe"},
            "NSS_ALLOW_HEALTHY_SERVER must be true or false",
        ),
        ({**BASE_ENV, **ONEAPI_ENV, "NSS_VM_USERNAME": "zsroot; rm -rf /"}, "NSS_VM_USERNAME is not a valid user name"),
        ({**BASE_ENV, **ONEAPI_ENV, "NSS_VM_PORT": "ssh"}, "NSS_VM_PORT must be a positive whole number"),
    ],
)
def test_settings_are_validated(env, expected):
    with pytest.raises(automation.AutomationError) as raised:
        automation.read_settings(env)
    assert expected in str(raised.value)


def test_main_reports_errors_without_secrets_or_traceback(monkeypatch, capsys, fake_vm):
    for name, value in {**BASE_ENV, **ONEAPI_ENV, "NSS_VM_HOST": "127.0.0.1", "NSS_VM_PORT": str(fake_vm.port)}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("NSS_VM_PASSWORD", VM_PASSWORD)

    def failing_factory(settings):
        raise RuntimeError(f"library failure mentioning {VM_PASSWORD}")

    monkeypatch.setattr(automation, "connect_zia", failing_factory)

    assert automation.main() == 1
    printed = capsys.readouterr()
    assert printed.err == "ERROR: RuntimeError: library failure mentioning ********\n"
    assert printed.out.endswith("==> The run failed.\n")
    assert VM_PASSWORD not in printed.out + printed.err
    assert "Traceback" not in printed.err


def test_main_writes_the_deployment_script_outputs(monkeypatch, tmp_path, capsys):
    output_path = tmp_path / "scriptoutputs.json"
    for name, value in {**BASE_ENV, **ONEAPI_ENV, "AZ_SCRIPTS_OUTPUT_PATH": str(output_path)}.items():
        monkeypatch.setenv(name, value)
    outputs = {"nssServerId": "1", "nssServerName": "nss", "nssServerCreated": True, "certificateInstalled": True}
    monkeypatch.setattr(automation, "run", lambda settings: outputs)

    assert automation.main() == 0
    assert automation.json.loads(output_path.read_text()) == outputs


# --------------------------------------------------------------------------------------
# The certificate is downloaded by the SDK function only
# --------------------------------------------------------------------------------------


def _client_for(api):
    client = Mock()
    client.zia.nss_servers = api
    return client


def test_download_calls_the_sdk_method():
    expected = certificate_zip()
    api = Mock(spec=["nss_download_certificate"])
    api.nss_download_certificate.return_value = (expected, Mock(), None)

    assert automation.download_certificate(_client_for(api), "7") == expected

    api.nss_download_certificate.assert_called_once_with("7")


def test_download_fails_with_the_minimum_version_when_the_sdk_has_no_download_method():
    # Releases before 1.11.5: the NSS servers API has no nss_download_certificate.
    api = Mock(spec=["_request_executor", "_zia_base_endpoint"])

    with pytest.raises(automation.AutomationError) as raised:
        automation.download_certificate(_client_for(api), "7")

    assert "has no nss_download_certificate" in str(raised.value)
    assert "Install zscaler-sdk-python 1.11.5 or later." in str(raised.value)
    assert api._request_executor.mock_calls == []


def test_download_reports_the_error_of_the_sdk_function():
    api = Mock(spec=["nss_download_certificate"])
    api.nss_download_certificate.return_value = (None, Mock(), ValueError("RESOURCE_NOT_FOUND"))

    with pytest.raises(automation.AutomationError, match="RESOURCE_NOT_FOUND"):
        automation.download_certificate(_client_for(api), "7")


def test_the_script_sends_no_request_of_its_own():
    source = SCRIPT.read_text(encoding="utf-8")

    for forbidden in ("nssDownload", "create_request", "_request_executor", "return_raw_response", "import requests"):
        assert forbidden not in source


def test_the_pinned_sdk_release_has_the_download_function():
    wrapper = (SCRIPT.parent / "install-nss-certificate.sh").read_text(encoding="utf-8")
    pinned = re.search(r"'zscaler-sdk-python==(\d+)\.(\d+)\.(\d+)'", wrapper)

    assert pinned, "install-nss-certificate.sh must pin zscaler-sdk-python to an exact version"
    minimum = tuple(int(part) for part in automation.MINIMUM_SDK_VERSION.split("."))
    assert automation.MINIMUM_SDK_VERSION == "1.11.5"
    assert tuple(int(part) for part in pinned.groups()) >= minimum


# --------------------------------------------------------------------------------------
# Real zscaler-sdk-python, HTTP intercepted
# --------------------------------------------------------------------------------------

API = "https://api.zsapi.net/zia/api/v1"


@pytest.fixture
def zia_http():
    pytest.importorskip("zscaler")
    responses = pytest.importorskip("responses")
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        mock.post(
            "https://acme.zslogin.net/oauth2/v1/token",
            json={"access_token": NOT_A_ACCESS_TOKEN, "token_type": "Bearer", "expires_in": 3600},
        )
        mock.get(f"{API}/nssServers", json=[{"id": 5, "name": "other", "status": "ENABLED", "type": "NSS_FOR_WEB"}])
        mock.post(
            f"{API}/nssServers",
            json={"id": 25235, "name": "Zscaler-NSS-VM", "status": "ENABLED", "type": "NSS_FOR_WEB"},
            status=201,
        )
        mock.get(f"{API}/nssDownload/25235", body=certificate_zip(), content_type="application/zip")
        # Never expected. They are answered so that a call would be recorded instead of failing.
        mock.post(f"{API}/status/activate", json={"status": "ACTIVE"})
        mock.delete(f"{API}/authenticatedSession", status=200, json={})
        yield mock


def _zia_calls(mock):
    return [
        f"{call.request.method} {call.request.url.split('?')[0]}"
        for call in mock.calls
        if "zslogin" not in call.request.url
    ]


def test_the_installed_sdk_has_the_download_method():
    sdk_module = pytest.importorskip("zscaler.zia.nss_servers")

    assert hasattr(sdk_module.NssServersAPI, "nss_download_certificate"), (
        "the installed zscaler-sdk-python has no NssServersAPI.nss_download_certificate; "
        f"the script needs {automation.MINIMUM_SDK_VERSION} or later"
    )


def test_workflow_through_the_real_sdk(fake_vm, zia_http):
    outputs = automation.run(settings_for(fake_vm))

    assert outputs["nssServerId"] == "25235" and outputs["nssServerCreated"] is True
    assert outputs["certificateInstalled"] is True
    assert _zia_calls(zia_http) == [f"GET {API}/nssServers", f"POST {API}/nssServers", f"GET {API}/nssDownload/25235"]
    post = next(
        call.request
        for call in zia_http.calls
        if call.request.url == f"{API}/nssServers" and call.request.method == "POST"
    )
    created = automation.json.loads(post.body)
    assert created == {"name": "Zscaler-NSS-VM", "status": "ENABLED", "type": "NSS_FOR_WEB"}
    assert zipfile.is_zipfile(fake_vm.home / "NssCertificate.zip")


def test_an_existing_server_through_the_real_sdk(fake_vm, zia_http):
    responses = pytest.importorskip("responses")
    zia_http.replace(
        responses.GET,
        f"{API}/nssServers",
        json=[{"id": 25235, "name": "Zscaler-NSS-VM", "status": "ENABLED", "type": "NSS_FOR_WEB"}],
    )

    outputs = automation.run(settings_for(fake_vm))

    assert outputs["nssServerId"] == "25235" and outputs["nssServerCreated"] is False
    assert _zia_calls(zia_http) == [f"GET {API}/nssServers", f"GET {API}/nssDownload/25235"]


def test_an_api_error_on_the_download_through_the_real_sdk_is_not_installed(fake_vm, zia_http):
    responses = pytest.importorskip("responses")
    zia_http.replace(
        responses.GET,
        f"{API}/nssDownload/25235",
        json={"code": "RESOURCE_NOT_FOUND", "message": "NSS server not found"},
        status=404,
    )

    with pytest.raises(automation.AutomationError, match="Downloading the certificate of NSS server 25235 failed"):
        automation.run(settings_for(fake_vm))

    assert fake_vm.installs == 0
    assert not (fake_vm.home / "NssCertificate.zip").exists()


LEGACY_API = "https://zsapi.zscaler.net/api/v1"


def test_workflow_through_the_real_sdk_with_legacy_credentials(fake_vm):
    pytest.importorskip("zscaler")
    responses = pytest.importorskip("responses")
    legacy = {
        "ZSCALER_CLIENT_ID": "",
        "ZSCALER_CLIENT_SECRET": "",
        "ZSCALER_VANITY_DOMAIN": "",
        "ZIA_USERNAME": "admin@example.com",
        "ZIA_PASSWORD": NOT_A_SECRET,
        "ZIA_API_KEY": NOT_A_SECRET,
        "ZIA_CLOUD": "zscaler",
        "NSS_SERVER_TYPE": "NSS_FOR_FIREWALL",
    }

    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        mock.post(
            f"{LEGACY_API}/authenticatedSession",
            json={"authType": "ADMIN_LOGIN"},
            headers={"Set-Cookie": "JSESSIONID=session-1; Path=/; Secure; HttpOnly"},
        )
        # The legacy client checks its session before each request.
        mock.get(f"{LEGACY_API}/authenticatedSession", json={"authType": "ADMIN_LOGIN"})
        mock.get(f"{LEGACY_API}/nssServers", json=[])
        mock.post(
            f"{LEGACY_API}/nssServers",
            json={"id": 31, "name": "Zscaler-NSS-VM", "status": "ENABLED", "type": "NSS_FOR_FIREWALL"},
        )
        mock.get(f"{LEGACY_API}/nssDownload/31", body=certificate_zip(), content_type="application/zip")
        # Never expected. They are answered so that a call would be recorded instead of failing.
        mock.post(f"{LEGACY_API}/status/activate", json={"status": "ACTIVE"})
        mock.delete(f"{LEGACY_API}/authenticatedSession", status=204)

        outputs = automation.run(settings_for(fake_vm, **legacy))

        calls = [f"{call.request.method} {call.request.url.split('?')[0]}" for call in mock.calls]
        calls = [call for call in calls if call != f"GET {LEGACY_API}/authenticatedSession"]

    assert outputs["nssServerId"] == "31" and outputs["certificateInstalled"] is True
    assert calls == [
        f"POST {LEGACY_API}/authenticatedSession",
        f"GET {LEGACY_API}/nssServers",
        f"POST {LEGACY_API}/nssServers",
        f"GET {LEGACY_API}/nssDownload/31",
    ]
