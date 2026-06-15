from __future__ import annotations

from pathlib import Path

import click

from backup_setuper import bootstrap as bs
from backup_setuper.config import ConfigError, load_machine, load_secrets
from backup_setuper.render import render_all, write_bundle


def _load(machine_path: Path, secrets_path: Path):
    try:
        secrets = load_secrets(secrets_path)
        return load_machine(machine_path, secrets)
    except ConfigError as e:
        raise click.ClickException(str(e))


secrets_opt = click.option(
    "--secrets", "secrets_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=Path("secrets.yaml"),
    show_default=True,
    help="Path to secrets.yaml.",
)

sudo_password_opt = click.option(
    "--sudo-password", "sudo_password",
    default=None,
    help="Sudo password for the target. If omitted and target.sudo is true, "
         "$BACKUP_SETUPER_SUDO_PASSWORD is used, else you are prompted.",
)


@click.group()
@click.version_option()
def main() -> None:
    """Bootstrap restic backups on a new server from your Mac."""


@main.command()
@click.argument("machine_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@secrets_opt
@sudo_password_opt
@click.option("--dry-run", is_flag=True, help="Render scripts and print them; no SSH activity.")
def bootstrap(machine_path: Path, secrets_path: Path, sudo_password: str | None, dry_run: bool) -> None:
    """End-to-end setup: render, push to target, init repos, install Hetzner keys."""
    machine = _load(machine_path, secrets_path)
    bs.bootstrap(machine, dry_run=dry_run, sudo_password=sudo_password)


@main.command()
@click.argument("machine_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@secrets_opt
@click.option("--out", "out_dir", type=click.Path(file_okay=False, path_type=Path), required=True,
              help="Directory to write rendered files into.")
def render(machine_path: Path, secrets_path: Path, out_dir: Path) -> None:
    """Render the bundle locally to a directory (no SSH)."""
    machine = _load(machine_path, secrets_path)
    bundle = render_all(machine)
    write_bundle(bundle, out_dir)
    click.echo(f"wrote {len(bundle.backup_scripts) + len(bundle.prune_scripts) + 1} files to {out_dir}")


@main.command()
@click.argument("machine_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@secrets_opt
@sudo_password_opt
def init_repos(machine_path: Path, secrets_path: Path, sudo_password: str | None) -> None:
    """Run `restic init` for each destination, idempotently."""
    bs.init_repos(_load(machine_path, secrets_path), sudo_password=sudo_password)


@main.command("hetzner-keys")
@click.argument("machine_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@secrets_opt
@sudo_password_opt
def hetzner_keys(machine_path: Path, secrets_path: Path, sudo_password: str | None) -> None:
    """Install the target's pubkey on each Hetzner box in the config."""
    bs.hetzner_keys(_load(machine_path, secrets_path), sudo_password=sudo_password)


@main.command("hetzner-revoke")
@click.argument("machine_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@secrets_opt
@sudo_password_opt
@click.option("--pubkey-file", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Read the pubkey from a local file instead of fetching it from the target.")
def hetzner_revoke(machine_path: Path, secrets_path: Path, sudo_password: str | None, pubkey_file: Path | None) -> None:
    """Remove the target's pubkey from each Hetzner box in the config."""
    pubkey = pubkey_file.read_text().strip() if pubkey_file else None
    bs.hetzner_revoke(_load(machine_path, secrets_path), pubkey=pubkey, sudo_password=sudo_password)


if __name__ == "__main__":
    main()
