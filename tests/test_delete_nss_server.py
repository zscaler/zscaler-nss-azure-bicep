"""Tests for scripts/delete-nss-server.py.

Nothing here talks to Zscaler. ZIA is a fake client, or the real zscaler-sdk-python with its
HTTP calls intercepted. The tests prove what the script asks the SDK to do, not how ZIA
answers a deletion.

Run with:  python -m pytest tests
Needs:     pytest; the tests that use the real SDK also need zscaler-sdk-python 1.11.5 or later
           and responses.
"""

import importlib.util
import re
from pathlib import Path
from unittest.mock import Mock

import pytest

from non_secrets import NOT_A_ACCESS_TOKEN, NOT_A_CLIENT_ID, NOT_A_SECRET

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


deletion = load("delete-nss-server")
certificate = load("install-nss-certificate")

CLIENT_SECRET = NOT_A_SECRET
ONEAPI = {
    "ZSCALER_CLIENT_ID": NOT_A_CLIENT_ID,
    "ZSCALER_CLIENT_SECRET": CLIENT_SECRET,
    "ZSCALER_VANITY_DOMAIN": "acme",
}
LEGACY = {
    "ZIA_USERNAME": "admin@example.com",
    "ZIA_PASSWORD": NOT_A_SECRET,
    "ZIA_API_KEY": NOT_A_SECRET,
    "ZIA_CLOUD": "zscaler",
}
SERVER = {"id": 28264, "name": "Zscaler-NSS-VM", "status": "ENABLED", "state": "HEALTHY", "type": "NSS_FOR_WEB"}
OTHER = {"id": 5, "name": "other", "status": "ENABLED", "state": "HEALTHY", "type": "NSS_FOR_WEB"}


class FakeZia:
    """Stands in for the zscaler-sdk-python client: same call shapes, state kept in memory."""

    def __init__(self, servers=(SERVER, OTHER), list_error=None, delete_error=None):
        self.servers = [dict(server) for server in servers]
        self.deleted = []
        self.closed = 0
        self.delete_error = delete_error
        self.zia = Mock()
        self.zia.nss_servers.list_nss_servers.side_effect = lambda: (
            (None, Mock(), list_error) if list_error else (list(self.servers), Mock(), None)
        )
        self.zia.nss_servers.delete_nss_server.side_effect = self._delete

    def _delete(self, nss_id):
        self.deleted.append(nss_id)
        if self.delete_error:
            return (None, Mock(), self.delete_error)
        self.servers = [server for server in self.servers if server["id"] != nss_id]
        return (None, Mock(), None)

    def __exit__(self, *args):
        self.closed += 1


def settings(**overrides):
    env = {"NSS_SERVER_ID": "28264", "NSS_SERVER_NAME": "Zscaler-NSS-VM", **ONEAPI}
    env.update(overrides)
    return deletion.read_settings(env)


def run(zia, **overrides):
    return deletion.run(settings(**overrides), zia_client_factory=Mock(return_value=zia))


# --------------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------------


def test_the_id_is_required():
    with pytest.raises(deletion.AutomationError, match="Required settings are missing: NSS_SERVER_ID\\."):
        settings(NSS_SERVER_ID=" ")


@pytest.mark.parametrize("value", ["abc", "28264; rm", "-1", "28264.0", "0x10"])
def test_an_id_that_is_not_a_number_is_refused(value):
    with pytest.raises(deletion.AutomationError, match="NSS_SERVER_ID must be the ID"):
        settings(NSS_SERVER_ID=value)


def test_the_name_is_optional():
    assert settings(NSS_SERVER_NAME="")["server_name"] == ""


def test_credentials_are_required():
    with pytest.raises(deletion.AutomationError, match="Zscaler credentials: either OneAPI"):
        deletion.read_settings({"NSS_SERVER_ID": "28264"})


CREDENTIAL_SETS = [
    ONEAPI,
    {**ONEAPI, "ZSCALER_CLIENT_SECRET": "", "ZSCALER_PRIVATE_KEY": NOT_A_SECRET},
    {**ONEAPI, "ZSCALER_CLOUD": "beta"},
    {**ONEAPI, "ZSCALER_VANITY_DOMAIN": ""},
    {**ONEAPI, "ZSCALER_CLIENT_SECRET": ""},
    LEGACY,
    {**LEGACY, "ZIA_PASSWORD": ""},
    {**LEGACY, "ZIA_API_KEY": "", "ZIA_CLOUD": ""},
    {**ONEAPI, **LEGACY},
    {"ZSCALER_VANITY_DOMAIN": "acme", "ZSCALER_CLIENT_SECRET": CLIENT_SECRET},
    {},
]


@pytest.mark.parametrize("credentials", CREDENTIAL_SETS)
def test_the_credentials_are_read_as_the_certificate_script_reads_them(credentials):
    """Both scripts hand their settings to the same connect_zia, so they must agree on them."""
    certificate_env = {
        "NSS_VM_HOST": "10.0.0.4",
        "NSS_VM_PASSWORD": NOT_A_SECRET,
        "NSS_SERVER_NAME": "n",
        **credentials,
    }
    deletion_env = {"NSS_SERVER_ID": "1", **credentials}
    keys = (
        "auth",
        "client_id",
        "client_secret",
        "private_key",
        "vanity_domain",
        "cloud",
        "zia_username",
        "zia_password",
        "zia_api_key",
        "zia_cloud",
    )

    try:
        expected = certificate.read_settings(certificate_env)
    except certificate.AutomationError as error:
        with pytest.raises(deletion.AutomationError) as refused:
            deletion.read_settings(deletion_env)
        assert str(refused.value) == str(error)
        return

    read = deletion.read_settings(deletion_env)
    assert {key: read[key] for key in keys} == {key: expected[key] for key in keys}


# --------------------------------------------------------------------------------------
# Deletion
# --------------------------------------------------------------------------------------


def test_deletes_the_server_with_the_id(capsys):
    zia = FakeZia()

    result = run(zia)

    assert result == {"nssServerId": "28264", "nssServerDeleted": True}
    assert zia.deleted == [28264]
    assert zia.servers == [OTHER]
    output = capsys.readouterr().out
    assert "Deleting the NSS server 'Zscaler-NSS-VM' (ID 28264, state reported by ZIA: HEALTHY) in ZIA." in output
    assert "pending change in ZIA" in output and "does not activate" in output
    # The lines are for a terminal, without the mark of an Azure deployment script log.
    assert "==>" not in output


def test_the_session_is_not_closed_and_nothing_is_activated():
    zia = FakeZia()

    run(zia)

    assert zia.closed == 0
    called = [call[0] for call in zia.zia.mock_calls]
    assert called == ["nss_servers.list_nss_servers", "nss_servers.delete_nss_server"]


def test_a_server_with_another_name_is_not_deleted():
    zia = FakeZia()

    with pytest.raises(deletion.AutomationError, match="is named 'Zscaler-NSS-VM' in ZIA, not 'Renamed'"):
        run(zia, NSS_SERVER_NAME="Renamed")

    assert zia.deleted == []


def test_without_a_name_the_id_alone_decides():
    zia = FakeZia()

    assert run(zia, NSS_SERVER_NAME="")["nssServerDeleted"] is True
    assert zia.deleted == [28264]


def test_an_id_zia_does_not_have_is_not_an_error(capsys):
    zia = FakeZia(servers=[OTHER])

    result = run(zia)

    assert result == {"nssServerId": "28264", "nssServerDeleted": False}
    assert zia.deleted == []
    assert "ZIA has no NSS server with the ID 28264. Nothing is deleted in ZIA." in capsys.readouterr().out


def test_a_server_with_the_same_name_and_another_id_is_left_alone(capsys):
    zia = FakeZia(servers=[{**SERVER, "id": 777}, OTHER])

    result = run(zia)

    assert result["nssServerDeleted"] is False
    assert zia.deleted == []
    assert "An NSS server named 'Zscaler-NSS-VM' exists with another ID" in capsys.readouterr().out


def test_an_id_is_matched_as_a_whole():
    zia = FakeZia(servers=[{**SERVER, "id": 282640}, {**SERVER, "id": 2826}])

    assert run(zia)["nssServerDeleted"] is False
    assert zia.deleted == []


def test_servers_that_are_objects_are_read_too():
    server = Mock()
    server.id, server.name, server.state = 28264, "Zscaler-NSS-VM", None
    zia = FakeZia(servers=[])
    zia.zia.nss_servers.list_nss_servers.side_effect = lambda: ([server], Mock(), None)

    assert run(zia)["nssServerDeleted"] is True
    assert zia.deleted == [28264]


def test_a_failed_listing_deletes_nothing():
    zia = FakeZia(list_error="401 Unauthorized")

    with pytest.raises(deletion.AutomationError, match="Listing the NSS servers in ZIA failed: 401 Unauthorized"):
        run(zia)

    assert zia.deleted == []


def test_a_failed_deletion_is_reported():
    zia = FakeZia(delete_error="NSS server is in use by a feed")

    with pytest.raises(
        deletion.AutomationError,
        match=re.escape("Deleting the NSS server 'Zscaler-NSS-VM' (ID 28264) in ZIA failed: NSS server is in use"),
    ):
        run(zia)


def test_an_sdk_without_the_delete_function_is_reported():
    zia = FakeZia()
    zia.zia.nss_servers.delete_nss_server = None

    with pytest.raises(deletion.AutomationError, match="has no delete_nss_server. Install zscaler-sdk-python 1.11.5"):
        run(zia)


def test_a_python_without_the_sdk_is_reported():
    def no_sdk(_):
        raise ModuleNotFoundError("No module named 'zscaler'")

    with pytest.raises(deletion.AutomationError, match="zscaler-sdk-python package is not installed"):
        deletion.run(settings(), zia_client_factory=no_sdk)


# --------------------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------------------


def set_environment(monkeypatch, **values):
    for name in ("NSS_SERVER_ID", "NSS_SERVER_NAME", "ZSCALER_PRIVATE_KEY", "ZSCALER_CLOUD", *ONEAPI, *LEGACY):
        monkeypatch.delenv(name, raising=False)
    for name, value in {"NSS_SERVER_ID": "28264", **ONEAPI, **values}.items():
        monkeypatch.setenv(name, value)


def test_main_succeeds(monkeypatch, capsys):
    set_environment(monkeypatch)
    zia = FakeZia()
    monkeypatch.setattr(deletion.certificate_script, "connect_zia", lambda _: zia)

    assert deletion.main() == 0
    assert zia.deleted == [28264]
    assert capsys.readouterr().err == ""


def test_main_reports_a_missing_setting_and_fails(monkeypatch, capsys):
    set_environment(monkeypatch, NSS_SERVER_ID="")

    assert deletion.main() == 1
    captured = capsys.readouterr()
    assert captured.err == "ERROR: Required settings are missing: NSS_SERVER_ID.\n"
    assert captured.out == ""


def test_main_never_prints_a_secret_or_a_traceback(monkeypatch, capsys):
    set_environment(monkeypatch)

    def broken(_):
        raise RuntimeError(f"request with secret {CLIENT_SECRET}\nfailed")

    monkeypatch.setattr(deletion.certificate_script, "connect_zia", broken)

    assert deletion.main() == 1
    captured = capsys.readouterr()
    assert captured.err == "ERROR: RuntimeError: request with secret ******** failed\n"
    assert CLIENT_SECRET not in captured.out + captured.err
    assert "Traceback" not in captured.out + captured.err


def test_the_pinned_sdk_release_is_the_one_of_the_certificate_script():
    """Both deploy scripts install the release the certificate deployment script is tested with."""
    wrapper = (SCRIPTS / "install-nss-certificate.sh").read_text(encoding="utf-8")
    pinned = re.search(r"'(zscaler-sdk-python==[0-9.]+)'", wrapper).group(1)

    assert f'SDK_REQUIREMENT="{pinned}"' in (SCRIPTS / "deploy.sh").read_text(encoding="utf-8")
    assert f"$script:SdkRequirement = '{pinned}'" in (SCRIPTS / "deploy.ps1").read_text(encoding="utf-8")


# --------------------------------------------------------------------------------------
# Real zscaler-sdk-python, HTTP intercepted
# --------------------------------------------------------------------------------------

API = "https://api.zsapi.net/zia/api/v1"
LEGACY_API = "https://zsapi.zscaler.net/api/v1"


def test_the_installed_sdk_has_the_delete_function():
    sdk_module = pytest.importorskip("zscaler.zia.nss_servers")

    assert hasattr(sdk_module.NssServersAPI, "delete_nss_server")


@pytest.fixture
def zia_http():
    pytest.importorskip("zscaler")
    responses = pytest.importorskip("responses")
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        mock.post(
            "https://acme.zslogin.net/oauth2/v1/token",
            json={"access_token": NOT_A_ACCESS_TOKEN, "token_type": "Bearer", "expires_in": 3600},
        )
        mock.get(f"{API}/nssServers", json=[OTHER, SERVER])
        mock.delete(f"{API}/nssServers/28264", status=204)
        # Never expected. They are answered so that a call would be recorded instead of failing.
        mock.delete(f"{API}/nssServers/5", status=204)
        mock.post(f"{API}/status/activate", json={"status": "ACTIVE"})
        mock.delete(f"{API}/authenticatedSession", status=200, json={})
        yield mock


def zia_calls(mock):
    return [
        f"{call.request.method} {call.request.url.split('?')[0]}"
        for call in mock.calls
        if "zslogin" not in call.request.url
    ]


def test_deletion_through_the_real_sdk(zia_http):
    result = deletion.run(settings())

    assert result["nssServerDeleted"] is True
    assert zia_calls(zia_http) == [f"GET {API}/nssServers", f"DELETE {API}/nssServers/28264"]


def test_an_absent_id_through_the_real_sdk_sends_no_deletion(zia_http):
    result = deletion.run(settings(NSS_SERVER_ID="31"))

    assert result["nssServerDeleted"] is False
    assert zia_calls(zia_http) == [f"GET {API}/nssServers"]


def test_an_api_error_on_the_deletion_through_the_real_sdk_is_reported(zia_http):
    responses = pytest.importorskip("responses")
    zia_http.replace(
        responses.DELETE,
        f"{API}/nssServers/28264",
        json={"code": "INVALID_INPUT_ARGUMENT", "message": "NSS server is referenced by an NSS feed"},
        status=400,
    )

    with pytest.raises(deletion.AutomationError, match="Deleting the NSS server 'Zscaler-NSS-VM' .* failed"):
        deletion.run(settings())


def test_deletion_through_the_real_sdk_with_legacy_credentials():
    pytest.importorskip("zscaler")
    responses = pytest.importorskip("responses")
    legacy = {"ZSCALER_CLIENT_ID": "", "ZSCALER_CLIENT_SECRET": "", "ZSCALER_VANITY_DOMAIN": "", **LEGACY}

    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        mock.post(
            f"{LEGACY_API}/authenticatedSession",
            json={"authType": "ADMIN_LOGIN"},
            headers={"Set-Cookie": "JSESSIONID=session-1; Path=/; Secure; HttpOnly"},
        )
        # The legacy client checks its session before each request.
        mock.get(f"{LEGACY_API}/authenticatedSession", json={"authType": "ADMIN_LOGIN"})
        mock.get(f"{LEGACY_API}/nssServers", json=[SERVER])
        mock.delete(f"{LEGACY_API}/nssServers/28264", status=204)
        # Never expected. They are answered so that a call would be recorded instead of failing.
        mock.post(f"{LEGACY_API}/status/activate", json={"status": "ACTIVE"})
        mock.delete(f"{LEGACY_API}/authenticatedSession", status=204)

        result = deletion.run(settings(**legacy))

        calls = [f"{call.request.method} {call.request.url.split('?')[0]}" for call in mock.calls]
        calls = [call for call in calls if call != f"GET {LEGACY_API}/authenticatedSession"]

    assert result["nssServerDeleted"] is True
    assert calls == [
        f"POST {LEGACY_API}/authenticatedSession",
        f"GET {LEGACY_API}/nssServers",
        f"DELETE {LEGACY_API}/nssServers/28264",
    ]
