#!/usr/bin/env python3
"""Deletes the NSS server object of a deployment in ZIA.

`make destroy` (scripts/deploy.sh) and `scripts/deploy.ps1 Destroy` run this script on the
workstation, before they delete the Azure resources, with the ID the deployment recorded for
the NSS server object whose certificate it installed. The object is deleted with
delete_nss_server of zscaler-sdk-python, which this script needs in release 1.11.5 or later.

All input comes from environment variables:
    NSS_SERVER_ID    ID of the NSS server object. Required.
    NSS_SERVER_NAME  Name the object is expected to have. When it is set, an object with
                     that ID and another name is not deleted.
    ZSCALER_* or ZIA_*  The Zscaler API credentials, as for install-nss-certificate.py.

It is safe to run again: an ID that ZIA no longer has is reported and is not an error. No
other object is looked for in its place, so nothing is ever deleted by its name.

The script never activates the ZIA configuration. The deletion stays a pending change until
the user activates it in ZIA.

The connection to ZIA is the one of install-nss-certificate.py, which must be in the same
directory.
"""

import importlib.util
import os
import sys
from pathlib import Path


def _load_certificate_script():
    path = Path(__file__).resolve().parent / "install-nss-certificate.py"
    spec = importlib.util.spec_from_file_location("install_nss_certificate", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


certificate_script = _load_certificate_script()
AutomationError = certificate_script.AutomationError
# That script marks its log lines for the log of an Azure deployment script. This one prints
# to a terminal, and the lines of both come without the mark.
certificate_script.LOG_MARK = ""
log = certificate_script.log


def read_settings(env=None):
    """Reads and validates the environment variables. Empty values count as not set."""
    env = os.environ if env is None else env

    def text(name):
        return (env.get(name) or "").strip()

    # The names of the credentials are the ones connect_zia of install-nss-certificate.py reads.
    settings = {
        "server_id": text("NSS_SERVER_ID"),
        "server_name": text("NSS_SERVER_NAME"),
        "client_id": text("ZSCALER_CLIENT_ID"),
        "client_secret": env.get("ZSCALER_CLIENT_SECRET") or "",
        "private_key": env.get("ZSCALER_PRIVATE_KEY") or "",
        "vanity_domain": text("ZSCALER_VANITY_DOMAIN"),
        "cloud": text("ZSCALER_CLOUD"),
        "zia_username": text("ZIA_USERNAME"),
        "zia_password": env.get("ZIA_PASSWORD") or "",
        "zia_api_key": env.get("ZIA_API_KEY") or "",
        "zia_cloud": text("ZIA_CLOUD"),
    }

    missing = [] if settings["server_id"] else ["NSS_SERVER_ID"]
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
    if not settings["server_id"].isdigit():
        raise AutomationError("NSS_SERVER_ID must be the ID of the NSS server, which is a number.")
    return settings


def run(settings, zia_client_factory=None):
    """Deletes the NSS server object and returns what was done."""
    zia_client_factory = zia_client_factory or certificate_script.connect_zia
    nss_id = settings["server_id"]
    expected_name = settings["server_name"]

    try:
        client = zia_client_factory(settings)
    except ImportError:
        raise AutomationError(
            "The zscaler-sdk-python package is not installed for this Python. "
            f"Install zscaler-sdk-python {certificate_script.MINIMUM_SDK_VERSION} or later."
        ) from None

    # The object is looked up before it is deleted, so that an ID ZIA no longer has is told
    # apart from a deletion that failed, and the name can be compared.
    servers, _, error = client.zia.nss_servers.list_nss_servers()
    if error:
        raise AutomationError(f"Listing the NSS servers in ZIA failed: {error}")
    servers = servers or []

    server = next((item for item in servers if str(certificate_script._field(item, "id")) == nss_id), None)
    if server is None:
        log(f"ZIA has no NSS server with the ID {nss_id}. Nothing is deleted in ZIA.")
        if expected_name and any(certificate_script._field(item, "name") == expected_name for item in servers):
            log(
                f"An NSS server named '{expected_name}' exists with another ID. It is not the one the "
                "deployment recorded and is left as it is."
            )
        return {"nssServerId": nss_id, "nssServerDeleted": False}

    name = certificate_script._field(server, "name")
    if expected_name and name != expected_name:
        raise AutomationError(
            f"The NSS server with the ID {nss_id} is named '{name}' in ZIA, not '{expected_name}'. "
            "It is not deleted, because it may not be the one the deployment created."
        )

    delete = getattr(client.zia.nss_servers, "delete_nss_server", None)
    if not callable(delete):
        raise AutomationError(
            f"The installed zscaler-sdk-python{certificate_script._installed_sdk_version()} has no "
            f"delete_nss_server. Install zscaler-sdk-python {certificate_script.MINIMUM_SDK_VERSION} or later."
        )

    state = certificate_script._field(server, "state")
    log(f"Deleting the NSS server '{name}' (ID {nss_id}, state reported by ZIA: {state or 'not reported'}) in ZIA.")
    _, _, error = delete(int(nss_id))
    if error:
        raise AutomationError(f"Deleting the NSS server '{name}' (ID {nss_id}) in ZIA failed: {error}")
    log(
        "The NSS server is deleted. The deletion is a pending change in ZIA. "
        "This script does not activate it; activate it in ZIA."
    )
    # The Zscaler session is deliberately not closed, for the reason install-nss-certificate.py
    # gives: the SDK ends a ZIA session that made a change in order to activate the staged
    # configuration, and activation is left to the user. The session expires on its own.
    return {"nssServerId": nss_id, "nssServerDeleted": True}


def main():
    settings = None
    try:
        settings = read_settings()
        run(settings)
    except AutomationError as error:
        return fail(f"ERROR: {error}", settings)
    except Exception as error:
        # No traceback: only the kind of failure and its message, without the secret values.
        return fail(f"ERROR: {type(error).__name__}: {error}", settings)
    return 0


def fail(message, settings):
    print(certificate_script.scrub(" ".join(message.split()), settings), file=sys.stderr, flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
