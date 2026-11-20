"""Stand-ins for the passwords, keys and tokens the tests need.

No test contacts Azure, Zscaler or a real NSS VM, so no value in the tests has to be a
credential, and none is. A test that needs something in the place of one calls not_a_secret()
instead of writing a text of its own: the value then says what it is wherever it turns up, and
no test file assigns a literal to a name such as a password or an API key, which is what a
secret scanner reports.

Never put a real credential into a test, and never read one from the environment for a test.
"""


def not_a_secret(purpose):
    """The value that stands in for a credential: "not-a-secret-<purpose>".

    The purpose keeps two stand-ins apart, so a test can tell which one a script passed on or
    printed.
    """
    return f"not-a-secret-{purpose}"
