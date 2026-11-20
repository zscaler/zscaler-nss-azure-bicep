"""The legacy client of the stand-in for zscaler-sdk-python. See the package."""

from zscaler import _Client


class LegacyZIAClient(_Client):
    kind = "legacy"
