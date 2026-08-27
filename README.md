# backup-setuper

One-command bootstrap of restic-based backups on a new server, from your Mac.

## Install

```bash
cd backup-setuper
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e . --no-deps
```

## SSH authentication

Connections use paramiko (via Fabric), not the `ssh` binary. paramiko honours only
`$SSH_AUTH_SOCK` and ignores `IdentityAgent` in `~/.ssh/config`, so keys held in an
external agent (1Password, Secretive, gpg-agent) used to fail with
`No authentication methods available` even though plain `ssh` worked.

The tool now reads `IdentityAgent` for the target host and sets `SSH_AUTH_SOCK` itself.
An `SSH_AUTH_SOCK` you set explicitly always wins. If your agent asks for confirmation,
approve the prompt — otherwise auth fails with `Authentication timeout.`

## Configure

1. Copy `secrets.example.yaml` to `secrets.yaml` and fill in real passphrases / Healthchecks UUIDs. `chmod 600 secrets.yaml`.
2. Copy `machines/silentbox.example.yaml` to `machines/<your-machine>.yaml` and edit:
   - `target.host` — SSH-addressable hostname (uses your `~/.ssh/config`).
   - `destinations` — keep only the ones this machine should write to. Each `name` becomes the script suffix on the target.

## Run

```bash
backup-setuper bootstrap machines/<your-machine>.yaml
```

This: pushes scripts to `/root/backup-scripts/`, writes `/root/.config/rclone/rclone.conf`, runs `restic init` on each destination, installs the target's SSH pubkey on each Hetzner Storage Box, prints the cron block to install with `crontab -e`.

Re-run anytime after editing the YAML — every step is idempotent.

## Other commands

```bash
backup-setuper render machines/silentbox.yaml --out ./out/      # dry-run, write files locally
backup-setuper init-repos machines/silentbox.yaml               # restic init step only
backup-setuper hetzner-keys machines/silentbox.yaml             # install Hetzner keys only
backup-setuper hetzner-revoke machines/silentbox.yaml           # remove target's key from each Hetzner box
backup-setuper check machines/silentbox.yaml                    # restic check + password-drift check
backup-setuper wipe machines/silentbox.yaml -d ftp              # DESTROY one repo and re-init it
```

`wipe` is irreversible and asks you to type the destination name to confirm. rclone
destinations only — Hetzner Storage Boxes are refused on purpose. Use it to rebuild a
repo from scratch, e.g. after changing `pack_size_mib` (see below), since restic's pack
size only applies to newly written pack files.

## Destinations with a file-count quota

Some backends cap the *number* of files, not just the bytes — Online.net's dedibackup
space allows 1000. restic's default 16 MiB packs blow through that fast, so a destination
can override two things:

```yaml
  - name: ftp
    kind: rclone-ftp
    pack_size_mib: 128        # restic --pack-size, 1..128 (128 is restic's hard maximum)
    retention: '--keep-daily 7 --keep-weekly 4 --keep-monthly 3'   # overrides restic.retention
```

- `pack_size_mib` is exported as `RESTIC_PACK_SIZE` in both the backup and prune scripts,
  because restic requires it on every repository-modifying command.
- `retention` overrides the machine-global `restic.retention` for this destination only.
  It is interpolated unquoted into the generated bash, so shell metacharacters are
  rejected at config-load time.

Note the arithmetic before relying on this: 1000 files × 128 MiB ≈ 120 GB is the hard
ceiling for such a destination. Pack size gets a repo under a file cap; retention is what
keeps it there.

## Tests

```bash
pytest -q
```
