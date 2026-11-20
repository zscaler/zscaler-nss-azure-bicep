"""Obvious non-secret values for tests. Never use real credentials in the repository."""

# A single label every fixture can share when the value is only passed through mocks.
NOT_A_SECRET = "not-a-secret"

# Values that must satisfy script checks (for example a SAS token needs a sig= part).
NOT_A_SAS_TOKEN = "not-a-sas-token&sig=not-a-secret"
NOT_A_CLIENT_ID = "not-a-client-id"

# Distinct typed answers in pseudo-terminal tests (still not credential-shaped).
NOT_A_TYPED_SAS = NOT_A_SAS_TOKEN
NOT_A_TYPED_VM_PASSWORD = "not-a-secret-vm-typed"
NOT_A_TYPED_CLIENT_ID = "not-a-client-id-typed"
NOT_A_TYPED_CLIENT_SECRET = "not-a-secret-client-typed"
NOT_A_TYPED_ZIA_PASSWORD = "not-a-secret-zia-typed"
NOT_A_TYPED_ZIA_API_KEY = "not-a-secret-api-key-typed"

# OAuth-style JSON bodies in intercepted HTTP tests.
NOT_A_ACCESS_TOKEN = "not-a-access-token"

# Wrong passwords in negative SSH tests.
NOT_A_WRONG_VM_PASSWORD = "not-a-secret-wrong"
NOT_A_OTHER_SUDO_PASSWORD = "not-a-secret-other"