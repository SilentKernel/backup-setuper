#!/usr/bin/env bash
# Sync gitignored configs (secrets.yaml + machines/*.yaml) to/from 1Password.
#
# Usage:
#   scripts/op-sync.sh push     # local -> 1Password (create or update each file)
#   scripts/op-sync.sh pull     # 1Password -> local
#   scripts/op-sync.sh list     # show what's stored under the prefix
#
# Env overrides:
#   OP_VAULT   vault name (default: Private)
#   OP_PREFIX  title prefix (default: backup-setuper/)

set -euo pipefail

VAULT="${OP_VAULT:-Private}"
PREFIX="${OP_PREFIX:-backup-setuper/}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

command -v op >/dev/null || { echo "op CLI not found. brew install --cask 1password-cli" >&2; exit 1; }
op whoami >/dev/null 2>&1 || { echo "Not signed in to 1Password. Run: eval \$(op signin)" >&2; exit 1; }

# Files to sync: secrets.yaml + every machines/*.yaml that isn't an example.
collect_files() {
  [[ -f secrets.yaml ]] && printf '%s\n' secrets.yaml
  for f in machines/*.yaml; do
    [[ -e $f ]] || continue
    [[ $f == *.example.yaml ]] && continue
    printf '%s\n' "$f"
  done
}

title_for() { printf '%s%s' "$PREFIX" "$1"; }

doc_id() {
  # Echo the document item id for $1 (title), or empty if absent.
  op document list --vault "$VAULT" --format json 2>/dev/null \
    | python3 -c "import json,sys; t=sys.argv[1]; print(next((d['id'] for d in json.load(sys.stdin) if d.get('title')==t), ''))" "$1"
}

push_one() {
  local file="$1" title id
  title="$(title_for "$file")"
  id="$(doc_id "$title")"
  if [[ -n $id ]]; then
    op document edit "$id" "$file" --vault "$VAULT" >/dev/null
    echo "updated  $title"
  else
    op document create "$file" --title "$title" --vault "$VAULT" >/dev/null
    echo "created  $title"
  fi
}

pull_one() {
  local file="$1" title
  title="$(title_for "$file")"
  if [[ -z "$(doc_id "$title")" ]]; then
    echo "missing  $title (skipped)" >&2
    return
  fi
  mkdir -p "$(dirname "$file")"
  op document get "$title" --vault "$VAULT" --out-file "$file" --force >/dev/null
  chmod 600 "$file"
  echo "pulled   $title -> $file"
}

cmd="${1:-}"
case "$cmd" in
  push)
    while IFS= read -r f; do push_one "$f"; done < <(collect_files)
    ;;
  pull)
    # Pull everything under the prefix so new machines added on another Mac land locally.
    op document list --vault "$VAULT" --format json \
      | python3 -c "
import json, sys
prefix = sys.argv[1]
for d in json.load(sys.stdin):
    t = d.get('title','')
    if t.startswith(prefix):
        print(t[len(prefix):])
" "$PREFIX" \
      | while IFS= read -r rel; do
          [[ -z $rel ]] && continue
          pull_one "$rel"
        done
    ;;
  list)
    op document list --vault "$VAULT" --format json \
      | python3 -c "
import json, sys
prefix = sys.argv[1]
for d in json.load(sys.stdin):
    t = d.get('title','')
    if t.startswith(prefix):
        print(t)
" "$PREFIX"
    ;;
  *)
    echo "Usage: $0 {push|pull|list}" >&2
    exit 2
    ;;
esac
