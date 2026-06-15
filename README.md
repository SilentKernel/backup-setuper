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
```

## Tests

```bash
pytest -q
```
