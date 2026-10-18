#!/usr/bin/env bash
# Enchaîne discard puis tiers (--cache-only) avec renouvellement auto du token.
# La mise en veille, capot fermé compris, est bloquée tant que ça tourne.
# Les arguments sont passés aux deux scripts, ex. : ./run_wikimasters.sh --cache-ttl 48
set -u
cd "$(dirname "$0")"
exec systemd-inhibit --what=sleep:handle-lid-switch --who=wikimasters \
    --why="Passage discard/tiers en cours" --mode=block \
    bash -c '
        .venv/bin/python -u wikimasters_discard.py --apply --max-cards 0 --auto-refresh "$@"
        .venv/bin/python -u wikimasters_tiers.py --rarities C,PC --apply --cache-only --auto-refresh "$@"
    ' run_wikimasters "$@"
