"""SSH wrappers for the target machine and Hetzner Storage Boxes.

These are thin facades over Fabric's Connection so bootstrap.py reads as a
flat sequence of steps and so tests can mock at a meaningful boundary.
"""
from __future__ import annotations

import io
import shlex
import uuid
from dataclasses import dataclass

from fabric import Config, Connection


# ---------- target machine ----------

class TargetSSH:
    def __init__(
        self,
        host: str,
        user: str = "root",
        ssh_key: str | None = None,
        port: int = 22,
        sudo: bool = False,
        sudo_password: str | None = None,
    ):
        connect_kwargs = {"key_filename": ssh_key} if ssh_key else None
        config = None
        if sudo:
            config = Config(overrides={"sudo": {"password": sudo_password}})
        self.conn = Connection(
            host=host, user=user, port=port,
            connect_kwargs=connect_kwargs, config=config,
        )
        self.host = host
        self.sudo = sudo

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.conn.close()

    def _run(self, cmd: str, **kw):
        """Route through sudo when configured. All callers use this; never .conn.run directly."""
        if self.sudo:
            return self.conn.sudo(cmd, **kw)
        return self.conn.run(cmd, **kw)

    # Public alias so callers outside this class don't need to know about the sudo branching.
    def run(self, cmd: str, **kw):
        return self._run(cmd, **kw)

    def has(self, binary: str) -> bool:
        return self._run(f"command -v {shlex.quote(binary)}", warn=True, hide=True).ok

    def ensure_dir(self, path: str, mode: str = "755") -> None:
        self._run(f"mkdir -p {shlex.quote(path)} && chmod {mode} {shlex.quote(path)}", hide=True)

    def write_file(self, remote_path: str, content: str, mode: str = "644") -> None:
        if not self.sudo:
            self.conn.put(io.StringIO(content), remote=remote_path)
            self.conn.run(f"chmod {mode} {shlex.quote(remote_path)}", hide=True)
            return
        # SFTP runs as the SSH user (not root) so land in /tmp first, then sudo-install.
        tmp = f"/tmp/bs-{uuid.uuid4().hex}"
        self.conn.put(io.StringIO(content), remote=tmp)
        # Always rm the tmp file, even on install failure.
        self._run(
            f"install -m {mode} -o root -g root {shlex.quote(tmp)} {shlex.quote(remote_path)}; "
            f"rc=$?; rm -f {shlex.quote(tmp)}; exit $rc",
            hide=True,
        )

    def ensure_ssh_key(self, path: str = "/root/.ssh/id_ed25519") -> str:
        """Make sure an ed25519 key exists at `path`; return its public key (one line)."""
        check = self._run(f"test -f {shlex.quote(path)}", warn=True, hide=True)
        if not check.ok:
            self._run(
                f"mkdir -p {shlex.quote('/root/.ssh')} && chmod 700 /root/.ssh && "
                f"ssh-keygen -t ed25519 -N '' -f {shlex.quote(path)} -C {shlex.quote('backup-setuper-' + self.host)}",
                hide=True,
            )
        pub = self._run(f"cat {shlex.quote(path + '.pub')}", hide=True).stdout.strip()
        return pub

    def read_file(self, remote_path: str) -> str | None:
        r = self._run(f"cat {shlex.quote(remote_path)}", warn=True, hide=True)
        return r.stdout if r.ok else None

    def list_files(self, remote_dir: str, pattern: str) -> list[str]:
        r = self._run(
            f"ls -1 {shlex.quote(remote_dir)}/{pattern} 2>/dev/null || true",
            warn=True, hide=True,
        )
        return [line.strip() for line in r.stdout.splitlines() if line.strip()]

    def rclone_obscure(self, cleartext: str) -> str:
        """Run `rclone obscure` on the target to encode a cleartext password."""
        r = self._run("rclone obscure -", in_stream=io.StringIO(cleartext + "\n"), hide=True)
        return r.stdout.strip()

    def restic_repo_exists(self, repo_url: str, password: str) -> bool:
        r = self._run(
            f"RESTIC_PASSWORD={shlex.quote(password)} "
            f"restic -r {shlex.quote(repo_url)} cat config",
            warn=True, hide=True,
        )
        return r.ok

    def restic_init(self, repo_url: str, password: str) -> None:
        self._run(
            f"RESTIC_PASSWORD={shlex.quote(password)} "
            f"restic -r {shlex.quote(repo_url)} init",
            hide=False,
        )


# ---------- Hetzner Storage Box ----------

@dataclass
class HetznerBoxRef:
    user: str
    host: str
    port: int = 23


def _key_body(line: str) -> str | None:
    """Return the base64 middle field of an OpenSSH authorized_keys line, or None if not parseable."""
    parts = line.strip().split()
    if len(parts) < 2 or not parts[0].startswith(("ssh-", "ecdsa-", "sk-")):
        return None
    return parts[1]


class HetznerBox:
    """Manage authorized_keys on a Hetzner Storage Box from the Mac.

    The Mac is assumed already trusted on the box (existing SSH key in agent /
    ~/.ssh/config). Add uses Hetzner's official `install-ssh-key` helper;
    remove edits ~/.ssh/authorized_keys directly via SFTP because Hetzner
    provides no documented remove command.
    """

    def __init__(self, ref: HetznerBoxRef):
        self.ref = ref
        self.conn = Connection(host=ref.host, user=ref.user, port=ref.port)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.conn.close()

    def list_key_bodies(self) -> set[str]:
        r = self.conn.run("cat ~/.ssh/authorized_keys 2>/dev/null || true", warn=True, hide=True)
        return {b for b in (_key_body(l) for l in r.stdout.splitlines()) if b}

    def install_key(self, pubkey: str) -> str:
        """Idempotent add. Returns 'installed' or 'already-present'."""
        body = _key_body(pubkey)
        if not body:
            raise ValueError(f"unparseable pubkey: {pubkey!r}")
        if body in self.list_key_bodies():
            return "already-present"
        # Hetzner's documented helper. It reads the pubkey from stdin.
        self.conn.run("install-ssh-key", in_stream=io.StringIO(pubkey + "\n"), hide=True)
        return "installed"

    def revoke_key(self, pubkey: str) -> str:
        """Remove pubkey from authorized_keys. Returns 'revoked', 'not-present', or 'refused'.

        Refuses to write back if removing the key would leave the file empty
        (safety: never lock yourself out via this tool).
        """
        body = _key_body(pubkey)
        if not body:
            raise ValueError(f"unparseable pubkey: {pubkey!r}")
        r = self.conn.run("cat ~/.ssh/authorized_keys 2>/dev/null || true", warn=True, hide=True)
        lines = [l for l in r.stdout.splitlines() if l.strip()]
        if not lines:
            return "not-present"
        kept = [l for l in lines if _key_body(l) != body]
        if len(kept) == len(lines):
            return "not-present"
        if not kept:
            return "refused"
        new_content = "\n".join(kept) + "\n"
        self.conn.put(io.StringIO(new_content), remote=".ssh/authorized_keys")
        self.conn.run("chmod 600 ~/.ssh/authorized_keys", hide=True)
        return "revoked"
