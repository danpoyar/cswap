"""Year-long inference token attached to a managed OAuth slot (CON-1329).

Why a second credential per slot: ``claude setup-token`` mints a one-year
OAuth token scoped ``user:inference`` only. It cannot expire mid-run, has no
refresh family to race over, and Claude Code prefers it over the stored
login when it arrives as ``CLAUDE_CODE_OAUTH_TOKEN`` (authentication docs,
precedence 5 over 7). But the same scope makes it blind to quota: the usage
endpoint answers ``403 OAuth token does not meet scope requirement
user:profile`` (live probe 2026-08-26). So the slot keeps its ordinary login
for identity and quota measurement, and sessions run their inference on the
attached token.

Storage: one base64 file per *identity* (email slug) under
``<backup>/tokens/``, mode 0600 — the token belongs to the account, not to
the slot number, so slot moves keep it and only an identity leaving the
account table drops it (``ClaudeAccountSwitcher._prune_mappings``).
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
from pathlib import Path

TOKENS_DIRNAME = "tokens"
# Matches Claude Code's own CLAUDE_CODE_OAUTH_TOKEN scope: inference only.
INFERENCE_TOKEN_SCOPES = ("user:inference",)
_TOKEN_PREFIX = "sk-ant-oat"


def _slug(email: str) -> str:
    from claude_swap.session import slugify_email

    return slugify_email(email)


def token_path(backup_dir: Path, email: str) -> Path:
    return backup_dir / TOKENS_DIRNAME / f"{_slug(email)}.enc"


def looks_like_inference_token(token: str | None) -> bool:
    """A raw ``sk-ant-oat…`` setup-token (never JSON, never an API key)."""
    if not token:
        return False
    text = token.strip()
    return text.startswith(_TOKEN_PREFIX) and not text.startswith("{")


def is_inference_token_credentials(credentials: str | None) -> bool:
    """Whether a credential JSON holds ONLY an inference token — no refresh
    family. A profile seeded this way owns no login generation: the backup
    login is the slot's family and quota gauge (collector, refresh)."""
    if not credentials:
        return False
    try:
        data = json.loads(credentials)
    except (ValueError, TypeError):
        return False
    if not isinstance(data, dict):
        return False
    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return False
    token = oauth.get("accessToken")
    return (
        isinstance(token, str)
        and token.startswith(_TOKEN_PREFIX)
        and not oauth.get("refreshToken")
    )


def read_inference_token(backup_dir: Path, email: str) -> str | None:
    """Attached token for an identity, or ``None`` (absent or unreadable)."""
    path = token_path(backup_dir, email)
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    if not raw:
        return None
    try:
        value = base64.b64decode(raw.encode("utf-8"), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    return value.strip() or None


def write_inference_token(backup_dir: Path, email: str, token: str) -> Path:
    """Atomically store the token (0600) for an identity; returns the path."""
    path = token_path(backup_dir, email)
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        os.chmod(path.parent, 0o700)
    encoded = base64.b64encode(token.strip().encode("utf-8")).decode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        os.write(fd, encoded.encode("utf-8"))
        os.close(fd)
        fd = -1
        os.replace(tmp, str(path))
        if sys.platform != "win32":
            os.chmod(str(path), 0o600)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def delete_inference_token(backup_dir: Path, email: str) -> bool:
    """Remove the attached token; ``True`` when something was removed."""
    path = token_path(backup_dir, email)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def inference_token_credentials(token: str) -> str:
    """Claude Code credential JSON that seeds a session profile with the token.

    Same shape ``add-token`` stores for a token-only account: no refresh
    token, no ``expiresAt`` — Claude Code reads it as an inference-only login
    (``tengu_oauth_tokens_inference_only``) and never tries to rotate it.
    """
    return json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": token.strip(),
                "scopes": list(INFERENCE_TOKEN_SCOPES),
            }
        }
    )
