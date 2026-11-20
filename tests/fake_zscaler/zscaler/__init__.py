"""A stand-in for zscaler-sdk-python, used by test_destroy.py.

The tests put this directory first on the module path of the Python that the deploy scripts
run scripts/delete-nss-server.py with. It keeps the NSS servers of a fake tenant in a file and
writes down what it was asked to do. It never contacts Zscaler.

The directory in FAKE_ZIA_DIR holds:
    servers.json   the NSS servers of the tenant
    calls.jsonl    one JSON object per call, appended by this module. The configuration a
                   client was created with is written with the SHA-256 of its secrets.
    list_error, delete_error   when present, the error the call of that name returns
"""

import hashlib
import json
import os
from pathlib import Path

SECRETS = ("clientSecret", "privateKey", "password", "api_key")


def _directory():
    return Path(os.environ["FAKE_ZIA_DIR"])


def _record(call):
    with (_directory() / "calls.jsonl").open("a", encoding="utf-8") as calls:
        calls.write(json.dumps(call) + "\n")
    # The fake Azure CLI writes to the same file, which gives the order of the two.
    if os.environ.get("FAKE_AZ_DIR"):
        with (Path(os.environ["FAKE_AZ_DIR"]) / "order").open("a", encoding="utf-8") as order:
            order.write(f"zia {call['call']}\n")


def _error(name):
    file = _directory() / f"{name}_error"
    return file.read_text(encoding="utf-8").strip() if file.exists() else None


class _NssServers:
    def list_nss_servers(self):
        _record({"call": "list"})
        if _error("list"):
            return (None, None, _error("list"))
        return (json.loads((_directory() / "servers.json").read_text(encoding="utf-8")), None, None)

    def delete_nss_server(self, nss_id):
        _record({"call": "delete", "id": nss_id})
        if _error("delete"):
            return (None, None, _error("delete"))
        file = _directory() / "servers.json"
        servers = json.loads(file.read_text(encoding="utf-8"))
        file.write_text(json.dumps([server for server in servers if server["id"] != nss_id]), encoding="utf-8")
        return (None, None, None)


class _Zia:
    nss_servers = _NssServers()


class _Client:
    kind = ""

    def __init__(self, config):
        shown = {
            key: hashlib.sha256(str(value).encode()).hexdigest() if key in SECRETS else value
            for key, value in config.items()
        }
        _record({"call": "connect", "client": self.kind, "config": shown})
        self.zia = _Zia()


class ZscalerClient(_Client):
    kind = "oneapi"
