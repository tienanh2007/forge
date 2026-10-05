class ForgeError(Exception):
    """Base error; the CLI prints the message and exits 1."""


class UsageError(ForgeError):
    pass


class NotFound(ForgeError):
    pass


class IntegrationError(ForgeError):
    """An external system (gh, SonarCloud, ClickUp, claude) failed. Messages never contain tokens."""
