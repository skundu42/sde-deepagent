"""Per-repo secrets for running setup/tests, kept away from the LLM.

A repo's `secrets` map references host environment variables by name, e.g.
`DATABASE_URL: env:BACKEND_DB_URL`. The actual values live only in the
operator's `.env`/secret manager — repos.yaml and the API only ever carry the
*reference*, never the value. The controller injects resolved values into the
registered setup/test commands (never into the agent's own shell), and a
`Redactor` masks the values from every output sink as defense in depth.

See the "Per-repo secrets" section of the README for the security model and the
honest residual risk (a determined prompt-injected agent that edits the code run
under test can still attempt exfiltration; mitigated by egress control, value
redaction, and the approval diff review)."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import threading
from pathlib import Path
from urllib.parse import quote

logger = logging.getLogger(__name__)

# Secret env-var names the agent's test/setup environment may define.
SECRET_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
# A reference is either `env:HOST_VAR` (value from the host environment) or the
# literal `store` (value held in the encrypted SecretStore, entered via the UI).
SECRET_REF_RE = re.compile(r"^(env:[A-Z_][A-Z0-9_]*|store)$")

# Names that must never be set as a repo secret: they would hijack the
# controlled execution environment, the shell, the dynamic loader, or git's
# ephemeral credential injection.
RESERVED_SECRET_NAMES = frozenset({
    "PATH", "HOME", "IFS", "SHELL", "USER", "LANG", "LC_ALL", "TMPDIR", "TERM",
    "BASH_ENV", "ENV", "PS1", "PS2", "PS4", "PROMPT_COMMAND", "PYTHONPATH",
    "PYTHONSTARTUP", "PYTHONHOME",
})

# Don't mask values shorter than this — too likely to collide with ordinary
# output and turn a trace into noise.
_MIN_REDACT_LEN = 5


def validate_secret_spec(secrets: dict[str, str]) -> None:
    """Raise ValueError if a repo's secrets map is malformed or unsafe.

    Keys are the env-var names exposed to the command; values are either an
    `env:HOST_VAR` reference (value from the host environment) or the literal
    `store` (value held in the encrypted SecretStore). Reserved/loader-sensitive
    names and `GIT_*`/`LD_*`/`DYLD_*` prefixes are rejected so a secret can't
    clobber the controlled environment."""
    if not isinstance(secrets, dict):
        raise ValueError("secrets must be a mapping of NAME -> 'env:HOST_VAR' or 'store'")
    for name, ref in secrets.items():
        if not isinstance(name, str) or not SECRET_NAME_RE.match(name):
            raise ValueError(
                f"invalid secret name {name!r}: must match [A-Z_][A-Z0-9_]*")
        if (name in RESERVED_SECRET_NAMES
                or name.startswith(("GIT_", "LD_", "DYLD_"))):
            raise ValueError(f"secret name {name!r} is reserved and cannot be set")
        if not isinstance(ref, str) or not SECRET_REF_RE.match(ref):
            raise ValueError(
                f"invalid reference for secret {name!r}: must be 'env:HOST_VAR' "
                "(value from the host env) or 'store' (value held encrypted)")


def resolve_repo_secrets(
    repo, environ: dict[str, str] | None = None, store: "SecretStore | None" = None
) -> tuple[dict[str, str], list[str]]:
    """Resolve a repo's secret references to concrete values.

    `env:HOST_VAR` references read the host environment; `store` references read
    the decrypted value from the encrypted SecretStore. Returns (resolved,
    missing): `resolved` maps each NAME to its value; `missing` lists the NAMES
    that couldn't be resolved (unset host var, or store value absent/unavailable)
    so the runner can warn without ever logging a value. Repos without secrets
    resolve to ({}, [])."""
    env = os.environ if environ is None else environ
    spec = getattr(repo, "secrets", None) or {}
    stored: dict[str, str] = {}
    if store is not None and getattr(store, "available", False) and any(
            ref == "store" for ref in spec.values()):
        stored = store.resolve(getattr(repo, "name", ""))
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for name, ref in spec.items():
        if ref == "store":
            value = stored.get(name)
        elif isinstance(ref, str) and ref.startswith("env:"):
            value = env.get(ref[len("env:"):])
        else:
            value = None
        if value:
            resolved[name] = value
        else:
            missing.append(name)
    return resolved, missing


class SecretStore:
    """Encrypted-at-rest store for UI-entered per-repo secret values.

    Each value is sealed with Fernet (symmetric authenticated encryption) under
    a data key derived from SECRETS_KEY, and persisted as a token in a 0600 JSON
    file (`data/secrets.enc`) — only the repo/secret NAMES are visible, never the
    values. Without SECRETS_KEY the store is `available == False` and refuses to
    store or resolve, so a repo's `store` secrets fail closed (the task warns and
    runs without them) rather than silently exposing anything.

    At-rest encryption protects backups, the data volume, and accidental commits;
    it does NOT defend against a host compromise that can also read SECRETS_KEY."""

    def __init__(self, path: Path | str, key: str | None) -> None:
        self.path = Path(path)
        self._fernet = self._make_fernet(key)
        self._lock = threading.Lock()

    @staticmethod
    def _make_fernet(key: str | None):
        if not key:
            return None
        from cryptography.fernet import Fernet
        # derive a valid 32-byte Fernet key from any high-entropy SECRETS_KEY
        data_key = base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest())
        return Fernet(data_key)

    @property
    def available(self) -> bool:
        return self._fernet is not None

    def _load(self) -> dict[str, dict[str, str]]:
        try:
            return json.loads(self.path.read_text())
        except Exception:  # noqa: BLE001 — missing/corrupt file: start empty
            return {}

    def _save(self, data: dict[str, dict[str, str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)

    def set_many(self, repo: str, values: dict[str, str]) -> None:
        if not self._fernet:
            raise RuntimeError("secret storage disabled: SECRETS_KEY is not set")
        with self._lock:
            data = self._load()
            bucket = data.setdefault(repo, {})
            for name, value in values.items():
                bucket[name] = self._fernet.encrypt(value.encode()).decode()
            self._save(data)

    def delete(self, repo: str, name: str | None = None) -> None:
        with self._lock:
            data = self._load()
            if repo not in data:
                return
            if name is None:
                data.pop(repo, None)
            else:
                data[repo].pop(name, None)
                if not data[repo]:
                    data.pop(repo, None)
            self._save(data)

    def names(self, repo: str) -> list[str]:
        """Secret NAMES stored for a repo (no decryption)."""
        return sorted(self._load().get(repo, {}))

    def resolve(self, repo: str) -> dict[str, str]:
        """Decrypt all stored values for a repo (NAME -> value). Empty if the
        store is unavailable; entries that fail to decrypt are dropped + logged."""
        if not self._fernet:
            return {}
        out: dict[str, str] = {}
        for name, token in self._load().get(repo, {}).items():
            try:
                out[name] = self._fernet.decrypt(token.encode()).decode()
            except Exception:  # noqa: BLE001 — wrong key / tampered token
                logger.warning("secret %r for repo %r could not be decrypted "
                               "(SECRETS_KEY changed?)", name, repo)
        return out


class Redactor:
    """Masks secret values (and common encodings of them) from any text.

    Built from the resolved secret values for one task. An empty Redactor is a
    fast identity, so secret-less repos pay nothing. Each value is masked in its
    raw, base64, and url-encoded forms; longest variants are replaced first so a
    substring match never pre-empts the full one."""

    def __init__(self, secrets: dict[str, str] | None = None) -> None:
        variants: dict[str, str] = {}
        for name, value in (secrets or {}).items():
            placeholder = f"«redacted:{name}»"
            for variant in self._variants_for(value):
                if len(variant) >= _MIN_REDACT_LEN:
                    variants.setdefault(variant, placeholder)
        # longest first: mask the most specific encoding before any substring
        self._variants: list[tuple[str, str]] = sorted(
            variants.items(), key=lambda kv: len(kv[0]), reverse=True)

    @staticmethod
    def _variants_for(value: str) -> set[str]:
        out = {value}
        try:
            out.add(base64.b64encode(value.encode()).decode())
        except Exception:  # noqa: BLE001 — encoding is best-effort
            pass
        try:
            out.add(quote(value, safe=""))
        except Exception:  # noqa: BLE001
            pass
        return out

    @property
    def active(self) -> bool:
        return bool(self._variants)

    def redact(self, text: str) -> str:
        if not text or not isinstance(text, str) or not self._variants:
            return text
        for variant, placeholder in self._variants:
            if variant in text:
                text = text.replace(variant, placeholder)
        return text

    def redact_obj(self, obj):
        """Redact every string inside a nested dict/list (e.g. an event payload)."""
        if not self._variants:
            return obj
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, dict):
            return {k: self.redact_obj(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.redact_obj(v) for v in obj]
        return obj
