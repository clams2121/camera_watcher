#!/usr/bin/env bash
# Updates this camera_watcher checkout in place and restarts every
# camera-watcher@ systemd instance on this host -- see the root README's
# "Updating" section and deploy/README.md.
#
# Deliberately conservative, the same way the removed web-UI self-update
# button used to be:
#   - Refuses to run against a dirty working tree at all.
#   - Fast-forward only -- never creates a merge commit or discards local
#     history. If the branch has diverged, it stops and tells you exactly
#     how to look into it, without touching anything else.
#   - Only restarts services after dependencies install cleanly. A failed
#     pip install leaves the old code still running under systemd and
#     prints a rollback recipe -- it never leaves the fleet mid-update.
#
# Usage:
#   ./update.sh              # fetch, fast-forward, reinstall deps, restart
#   ./update.sh --no-fetch    # skip fetch/merge -- just reinstall deps and restart
#                              # (e.g. after you've already updated the code some other way)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

NO_FETCH=0
for arg in "$@"; do
  case "$arg" in
    --no-fetch)
      NO_FETCH=1
      ;;
    *)
      echo "update.sh: unknown argument: $arg" >&2
      echo "usage: $0 [--no-fetch]" >&2
      exit 1
      ;;
  esac
done

log() {
  echo "update.sh: $*"
}

fail() {
  echo "update.sh: ERROR: $*" >&2
  exit 1
}

if [ ! -d "$REPO_ROOT/.git" ]; then
  fail "$REPO_ROOT is not a git checkout -- update.sh must live at the root of one (see deploy/README.md)."
fi

PYTHON="$REPO_ROOT/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  fail "$PYTHON not found or not executable. Expected a virtualenv at $REPO_ROOT/.venv -- see the README's Setup section."
fi

BEFORE_COMMIT="$(git rev-parse HEAD)"

# --- 1. Refuse a dirty tree outright -------------------------------------
if [ -n "$(git status --porcelain)" ]; then
  fail "$(cat <<EOF
working tree is not clean -- refusing to update.
Uncommitted changes were found in $REPO_ROOT. Commit, stash, or discard them first:
    git status
    git stash            # to set them aside, or
    git checkout -- .    # to discard them (careful -- this is destructive)
then re-run $0.
EOF
)"
fi

# --- 2. Fetch + fast-forward-only merge -----------------------------------
if [ "$NO_FETCH" -eq 0 ]; then
  CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
  if [ "$CURRENT_BRANCH" = "HEAD" ]; then
    fail "not on a branch (detached HEAD) -- update.sh only knows how to fast-forward a branch. Check out one first: git checkout <branch>."
  fi

  log "Fetching origin/$CURRENT_BRANCH..."
  if ! git fetch origin "$CURRENT_BRANCH"; then
    fail "git fetch failed -- check network connectivity and try again. Nothing was changed."
  fi

  log "Fast-forwarding $CURRENT_BRANCH to origin/$CURRENT_BRANCH..."
  if ! git merge --ff-only "origin/$CURRENT_BRANCH"; then
    fail "$(cat <<EOF
git merge --ff-only failed -- the local branch has diverged from origin/$CURRENT_BRANCH
(local commits that aren't upstream, or a history rewrite upstream). Nothing was changed.
Look into it manually, e.g.:
    git log --oneline HEAD..origin/$CURRENT_BRANCH   # what's upstream that you don't have
    git log --oneline origin/$CURRENT_BRANCH..HEAD   # what you have that upstream doesn't
This script deliberately never merges or rebases automatically here -- resolve it yourself,
then re-run $0.
EOF
)"
  fi

  AFTER_COMMIT="$(git rev-parse HEAD)"
  if [ "$BEFORE_COMMIT" = "$AFTER_COMMIT" ]; then
    log "Already up to date at $AFTER_COMMIT."
  else
    log "Updated $BEFORE_COMMIT -> $AFTER_COMMIT."
  fi
else
  log "--no-fetch given -- skipping fetch/merge, using the code already checked out."
fi

# --- 3. Reinstall dependencies ---------------------------------------------
log "Installing dependencies..."
if ! "$PYTHON" -m pip install -q -r "$REPO_ROOT/requirements.txt"; then
  fail "$(cat <<EOF
dependency install failed -- NOT restarting any service, so the fleet keeps running
whatever code it was already running. Rollback recipe, if you want the checkout back
to exactly where it was before this run:
    git reset --hard $BEFORE_COMMIT
    $0 --no-fetch
Otherwise, fix the dependency problem (see the pip output above) and re-run:
    $0 --no-fetch
EOF
)"
fi
log "Dependencies installed."

# --- 4. Restart every camera-watcher@ instance on this host -----------------
if ! command -v systemctl >/dev/null 2>&1; then
  log "systemctl not found -- skipping service restart (not a systemd host, or camera_watcher isn't deployed as a service here)."
  log "Update complete."
  exit 0
fi

mapfile -t UNITS < <(systemctl list-units --all --type=service --plain --no-legend 'camera-watcher@*.service' 2>/dev/null | awk '{print $1}')

if [ "${#UNITS[@]}" -eq 0 ]; then
  log "No camera-watcher@ service instances found on this host -- nothing to restart."
  log "Update complete."
  exit 0
fi

log "Restarting ${#UNITS[@]} camera-watcher@ instance(s): ${UNITS[*]}"
RESTART_FAILURES=0
for unit in "${UNITS[@]}"; do
  if sudo systemctl restart "$unit"; then
    log "  $unit: restarted"
  else
    log "  $unit: FAILED TO RESTART"
    RESTART_FAILURES=$((RESTART_FAILURES + 1))
  fi
done

log "Per-instance status:"
for unit in "${UNITS[@]}"; do
  STATE="$(systemctl is-active "$unit" 2>/dev/null || true)"
  log "  $unit: $STATE"
done

if [ "$RESTART_FAILURES" -gt 0 ]; then
  fail "$RESTART_FAILURES service(s) failed to restart -- see \`journalctl -u <unit>\` for each one listed above. Code was already updated; only the restart failed."
fi

log "Update complete."
