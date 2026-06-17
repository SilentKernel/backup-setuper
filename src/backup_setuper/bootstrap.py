from __future__ import annotations

import getpass
import os

import click

from backup_setuper.config import Machine
from backup_setuper.remote import HetznerBox, HetznerBoxRef, TargetSSH
from backup_setuper.render import (
    RenderedBundle,
    render_all,
    render_rclone_conf,
)

SCRIPTS_DIR = "/root/backup-scripts"
LOGS_DIR = "/root/backup-logs"
RCLONE_CONF = "/root/.config/rclone/rclone.conf"


def _step(msg: str) -> None:
    click.echo(click.style(f"==> {msg}", fg="cyan"))


def _ok(msg: str) -> None:
    click.echo(click.style(f"    {msg}", fg="green"))


def _warn(msg: str) -> None:
    click.echo(click.style(f"    {msg}", fg="yellow"))


def _resolve_sudo_password(machine: Machine, cli_value: str | None) -> str | None:
    """Resolve the sudo password from CLI flag, env, or interactive prompt — only if needed."""
    if not machine.target.sudo:
        return None
    if cli_value:
        return cli_value
    env = os.environ.get("BACKUP_SETUPER_SUDO_PASSWORD")
    if env:
        return env
    return getpass.getpass(f"[sudo] password for {machine.target.user}@{machine.target.host}: ")


def _open_target(machine: Machine, sudo_password: str | None) -> TargetSSH:
    return TargetSSH(
        host=machine.target.host,
        user=machine.target.user,
        ssh_key=machine.target.ssh_key,
        port=machine.target.port,
        sudo=machine.target.sudo,
        sudo_password=sudo_password,
    )


def bootstrap(machine: Machine, dry_run: bool = False, sudo_password: str | None = None) -> None:
    bundle = render_all(machine)

    if dry_run:
        _step("Dry-run: rendered scripts will be printed, no SSH activity")
        for name, content in {**bundle.backup_scripts, **bundle.prune_scripts}.items():
            click.echo(f"\n--- {name} ---\n{content}")
        click.echo(f"\n--- cron.txt ---\n{bundle.cron_txt}")
        return

    sudo_password = _resolve_sudo_password(machine, sudo_password)
    with _open_target(machine, sudo_password) as target:
        _step(f"Connected to {machine.target.user}@{machine.target.host}:{machine.target.port}"
              + (" (sudo)" if machine.target.sudo else ""))

        _step("Checking prerequisites on target")
        for binary in ("restic", "rclone", "ssh-keygen", "curl"):
            if not target.has(binary):
                raise click.ClickException(f"missing prerequisite on target: {binary}")
        _ok("restic, rclone, ssh-keygen, curl present")

        _step("Ensuring target SSH key")
        pubkey = target.ensure_ssh_key()
        _ok(f"pubkey: {pubkey[:60]}…")

        if machine.hetzner_destinations:
            _step("Installing target pubkey on Hetzner Storage Boxes")
            for d in machine.hetzner_destinations:
                ref = HetznerBoxRef(user=d.sftp.user, host=d.sftp.host, port=d.sftp.port)
                with HetznerBox(ref) as box:
                    status = box.install_key(pubkey)
                    _ok(f"{d.name}: {status}")

            _step("Trusting Hetzner host keys on target")
            for d in machine.hetzner_destinations:
                added = target.ensure_known_host(d.sftp.host, d.sftp.port)
                _ok(f"{d.name}: {'added to known_hosts' if added else 'already trusted'}")

        _step("Creating directories on target")
        target.ensure_dir(SCRIPTS_DIR)
        target.ensure_dir(LOGS_DIR)
        target.ensure_dir("/root/.config/rclone", mode="700")
        _ok(f"{SCRIPTS_DIR}, {LOGS_DIR}")

        _step("Uploading generated scripts")
        for name, content in {**bundle.backup_scripts, **bundle.prune_scripts}.items():
            target.write_file(f"{SCRIPTS_DIR}/{name}", content, mode="755")
        target.write_file(f"{SCRIPTS_DIR}/cron.txt", bundle.cron_txt)
        _ok(f"{len(bundle.backup_scripts) + len(bundle.prune_scripts)} scripts + cron.txt")

        if bundle.rclone_remotes:
            _step("Writing rclone.conf (passwords obscured on target)")
            obscured = []
            for r in bundle.rclone_remotes:
                cleartext = r.fields["pass"]
                fields = dict(r.fields)
                fields["pass"] = target.rclone_obscure(cleartext)
                obscured.append(type(r)(name=r.name, type=r.type, fields=fields))
            target.write_file(RCLONE_CONF, render_rclone_conf(obscured), mode="600")
            _ok(f"{len(obscured)} rclone remote(s) configured")

        _step("Pruning stale destination scripts on target")
        expected = set(bundle.backup_scripts) | set(bundle.prune_scripts) | {"cron.txt"}
        on_target = set()
        for pattern in ("backup-restic-*.sh", "prune-restic-*.sh"):
            for path in target.list_files(SCRIPTS_DIR, pattern):
                on_target.add(path.rsplit("/", 1)[-1])
        stale = sorted(on_target - expected)
        if stale:
            _warn(f"stale scripts on target: {', '.join(stale)}")
            if click.confirm("    delete them?", default=False):
                for s in stale:
                    target.run(f"rm {SCRIPTS_DIR}/{s}", hide=True)
                _ok(f"deleted {len(stale)} file(s)")
        else:
            _ok("nothing stale")

        _step("Initializing restic repositories")
        for d in machine.destinations:
            if target.restic_repo_exists(d.repository_url, machine.restic.password):
                _ok(f"{d.name}: already initialized")
            else:
                _ok(f"{d.name}: running restic init …")
                target.restic_init(d.repository_url, machine.restic.password)

        _step("Done. Install the cron block below with `crontab -e`:")
        click.echo("")
        click.echo(bundle.cron_txt)


def hetzner_keys(machine: Machine, sudo_password: str | None = None) -> None:
    sudo_password = _resolve_sudo_password(machine, sudo_password)
    with _open_target(machine, sudo_password) as target:
        pubkey = target.ensure_ssh_key()
        for d in machine.hetzner_destinations:
            ref = HetznerBoxRef(user=d.sftp.user, host=d.sftp.host, port=d.sftp.port)
            with HetznerBox(ref) as box:
                click.echo(f"{d.name}: {box.install_key(pubkey)}")


def hetzner_revoke(machine: Machine, pubkey: str | None = None, sudo_password: str | None = None) -> None:
    if pubkey is None:
        sudo_password = _resolve_sudo_password(machine, sudo_password)
        with _open_target(machine, sudo_password) as target:
            pubkey = target.ensure_ssh_key()
    for d in machine.hetzner_destinations:
        ref = HetznerBoxRef(user=d.sftp.user, host=d.sftp.host, port=d.sftp.port)
        with HetznerBox(ref) as box:
            click.echo(f"{d.name}: {box.revoke_key(pubkey)}")


def _script_password(content: str) -> str | None:
    """Extract the value of `export RESTIC_PASSWORD='...'` from a deployed backup script."""
    for line in content.splitlines():
        if line.startswith("export RESTIC_PASSWORD="):
            return line.split("'", 2)[1] if line.count("'") >= 2 else None
    return None


def check(
    machine: Machine,
    dest_name: str | None = None,
    read_data: bool = False,
    read_data_subset: str | None = None,
    sudo_password: str | None = None,
) -> None:
    """Run `restic check` on the target for each destination (or one).

    Also verifies the restic password baked into the deployed backup script
    matches the local config — catching config drift where the secret changed
    locally without re-running bootstrap.
    """
    if dest_name is not None:
        selected = [d for d in machine.destinations if d.name == dest_name]
        if not selected:
            valid = ", ".join(d.name for d in machine.destinations)
            raise click.ClickException(
                f"unknown destination {dest_name!r}; valid names: {valid}"
            )
    else:
        selected = machine.destinations

    failures = 0
    sudo_password = _resolve_sudo_password(machine, sudo_password)
    with _open_target(machine, sudo_password) as target:
        for d in selected:
            if d.kind == "hetzner-sftp":
                target.ensure_known_host(d.sftp.host, d.sftp.port)

        for d in selected:
            _step(f"Checking {d.label}")

            # Password sync: compare the deployed script's RESTIC_PASSWORD to local config.
            script = target.read_file(f"{SCRIPTS_DIR}/backup-restic-{d.name}.sh")
            if script is None:
                _warn("backup script not deployed on target (run bootstrap)")
                failures += 1
            elif _script_password(script) != machine.restic.password:
                _warn("restic password OUT OF SYNC with local config (re-run bootstrap)")
                failures += 1
            else:
                _ok("restic password in sync")

            if target.restic_check(
                d.repository_url, machine.restic.password,
                read_data=read_data, read_data_subset=read_data_subset,
            ):
                _ok("repository check passed")
            else:
                _warn("repository check FAILED")
                failures += 1

    if failures:
        raise click.ClickException(f"{failures} check(s) failed")


def init_repos(machine: Machine, sudo_password: str | None = None) -> None:
    sudo_password = _resolve_sudo_password(machine, sudo_password)
    with _open_target(machine, sudo_password) as target:
        for d in machine.hetzner_destinations:
            target.ensure_known_host(d.sftp.host, d.sftp.port)
        for d in machine.destinations:
            if target.restic_repo_exists(d.repository_url, machine.restic.password):
                click.echo(f"{d.name}: already initialized")
            else:
                click.echo(f"{d.name}: running restic init …")
                target.restic_init(d.repository_url, machine.restic.password)
