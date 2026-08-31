"""Custom exceptions for Claude Switch."""


class ClaudeSwitchError(Exception):
    """Base exception for Claude Switch errors."""

    pass


class CredentialError(ClaudeSwitchError):
    """Error related to credential operations."""

    pass


class CredentialReadError(CredentialError):
    """Failed to read credentials."""

    pass


class CredentialWriteError(CredentialError):
    """Failed to write credentials."""

    pass


class ConfigError(ClaudeSwitchError):
    """Error related to configuration operations."""

    pass


class SwitchError(ClaudeSwitchError):
    """Error during account switch operation."""

    pass


class LiveSessionRefusal(SwitchError):
    """A switch refused because a live ``cswap run`` session owns the slot's
    token family (CON-1579/CON-1595).

    The session's claude rotated the family past the stored backup, so
    activating the backup would land a dead login and sharing the session's
    copy would kill one of the two at the next refresh. The safe path to a
    terminal on that slot is ``cswap run N`` — carried here as data so a UI
    (TUI, menu bar) can offer or perform it instead of printing the text.
    Still a :class:`SwitchError`: every existing handler keeps catching it.
    """

    def __init__(
        self, message: str, *, account_num: str, email: str, pids: list[int]
    ) -> None:
        super().__init__(message)
        self.account_num = account_num
        self.email = email
        self.pids = list(pids)

    @property
    def command(self) -> str:
        """The recipe: a terminal on the slot, sharing the session profile."""
        return f"cswap run {self.account_num}"


class SessionError(ClaudeSwitchError):
    """Error setting up or launching a session-mode profile."""

    pass


class LockError(ClaudeSwitchError):
    """Error acquiring lock."""

    pass


class ClaudeCodeLockTimeout(LockError):
    """Timed out acquiring one of Claude Code's own advisory locks.

    Raised when ``~/.claude.lock`` / ``~/.claude.json.lock`` stays held past
    our bounded wait — usually Claude Code mid-token-refresh. Nothing has been
    mutated when this raises; the operation is safe to retry.
    """

    pass


class AccountNotFoundError(ClaudeSwitchError):
    """Account not found."""

    pass


class ValidationError(ClaudeSwitchError):
    """Validation error."""

    pass


class TransferError(ClaudeSwitchError):
    """Error during account export or import."""

    pass


class MigrationError(ClaudeSwitchError):
    """Error migrating the backup directory between layouts (e.g. legacy → XDG)."""

    pass


class MigrationIncomplete(ClaudeSwitchError):
    """A one-time data migration could not finish for every record.

    Raised by run-once migrations (see ``migrations.py``) when some entries
    failed or the source backend was inaccessible. The migration runner treats
    this as "not applied" so the migration is retried on the next run rather
    than being recorded as done with records left behind.
    """

    pass
