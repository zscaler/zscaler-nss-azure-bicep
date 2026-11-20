#!/usr/bin/env bash
# Runs in the Azure CLI container of the deployment script in modules/nss-certificate.bicep.
# It installs the Python packages the automation needs in a throwaway virtual environment and
# runs install-nss-certificate.py, whose source Bicep inserts in place of the placeholder line.
# All input is read from environment variables by the Python script.
set -euo pipefail

work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT

python3 -m venv "$work_dir/venv"

# install-nss-certificate.py downloads the certificate with nss_download_certificate of
# zscaler-sdk-python, which needs release 1.11.5 or later.
"$work_dir/venv/bin/python" -m pip install --quiet --disable-pip-version-check --no-cache-dir --retries 5 \
  'zscaler-sdk-python==1.11.5' \
  'paramiko==5.1.0'

cat > "$work_dir/install-nss-certificate.py" <<'NSS_CERTIFICATE_PYTHON_SOURCE'
__INSTALL_NSS_CERTIFICATE_PY__
NSS_CERTIFICATE_PYTHON_SOURCE

"$work_dir/venv/bin/python" "$work_dir/install-nss-certificate.py"
