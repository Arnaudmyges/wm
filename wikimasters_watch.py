#!/usr/bin/env python3
"""
WikiMasters : traite les cartes obtenues depuis le dernier passage (packs,
récompenses...). Chacune reçoit, selon son prix moyen du marché :
  - "discard" si prix <= seuil (jamais sur favoris, shiny, échanges en cours) ;
  - son palier de prix ("75+" ... "9600+") ;
  - "jamais vendu" si elle n'a encore aucune vente.
Un résumé est envoyé sur ntfy et/ou Discord (voir README.md).

Fait pour tourner régulièrement (timer systemd, cron) : la date de la dernière
carte traitée est gardée dans watch_state.json. Une carte en erreur prix est
reprise au passage suivant.

Par défaut en DRY-RUN : rien n'est modifié.

  python wikimasters_watch.py                      # dry-run, cartes des dernières 24 h au 1er passage
  python wikimasters_watch.py --apply --auto-refresh --notify
  python wikimasters_watch.py --since 2026-10-24T00:00:00Z   # rejoue depuis cette date

Voir README.md pour la configuration (.env).
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

from wikimasters_common import (DATA_DIR, ServerError, add_common_args, connect, has_tag, notify)
from wikimasters_tiers import COLORS, DEFAULT_TIERS

STATE_FILE = DATA_DIR / "watch_state.json"


def load_since(args):
    if args.since:
        return args.since
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))["since"]
    except (OSError, ValueError, KeyError):
        start = datetime.now(timezone.utc) - timedelta(hours=args.first_lookback)
        print(f"Pas d'état précédent : cartes obtenues depuis {args.first_lookback:g} h.")
        return start.isoformat()


def save_since(since):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"since": since}), encoding="utf-8")
    tmp.replace(STATE_FILE)


def run(args):
    tiers = sorted({float(t) for t in args.tiers.split(",") if t.strip()}, reverse=True)
    rarities = [r.strip() for r in args.rarities.split(",") if r.strip()]

    wm = connect(args.delay, args.auto_refresh)
    discard_tag = (args.discard_tag, wm.tag_id(args.discard_tag, create=args.create_tags, color="#9ca3af"))
    no_sales_tag = (args.no_sales_tag, wm.tag_id(args.no_sales_tag, create=args.create_tags, color="#f472b6"))
    tier_tags = {}
    for rank, t in enumerate(sorted(tiers)):
        name = f"{t:g}+"
        tier_tags[t] = (name, wm.tag_id(name, create=args.create_tags, color=COLORS[rank % len(COLORS)]))

    since = load_since(args)
    cards = wm.new_cards(since, rarities)
    print(f"Mode : {'APPLY' if args.apply else 'DRY-RUN'} | {len(cards)} carte(s) obtenue(s) après {since}\n")
    if not cards:
        if args.apply and not args.since:
            save_since(since)
        return 0

    tags = wm.card_tags(c["id"] for c in cards)
    pending = wm.pending_trades()

    done, new_since = [], since
    errors = stopped = 0
    for c in cards:
        if not wm.ensure_token():
            print("Token expiré et non renouvelé : arrêt, la suite au prochain passage.")
            stopped = 2
            break
        uc_id, card_id, r_code = c["id"], c["card_id"], c["snapshot_rarity"]
        name = c.get("snapshot_title") or card_id
        item = {"tags": tags.get(uc_id, [])}
        try:
            price = (wm.sales_summary(card_id).get(r_code) or {}).get("average")
        except ServerError as e:
            # date figée avant cette carte : elle et les suivantes seront reprises
            print(f"[error] {r_code:<3}    ?     {name}  (erreur prix : {e})")
            errors += 1
            if errors >= 3:
                stopped = 3
                break
            continue

        # (tag à poser, tags de palier périmés à retirer, libellé)
        action = None
        if price is None:
            if not has_tag(item, no_sales_tag[1], no_sales_tag[0]):
                action = (no_sales_tag, [], "jamais vendu")
        elif args.max_price and price > args.max_price:
            pass  # moyenne aberrante : ni palier ni discard
        elif price <= args.threshold:
            protected = c.get("starred") or (c.get("is_shiny") and not args.include_shiny) \
                or uc_id in pending or card_id in pending
            if not protected and not has_tag(item, discard_tag[1], discard_tag[0]):
                action = (discard_tag, [], "discard")
        else:
            tier = next((t for t in tiers if price >= t), None)
            if tier is not None:
                stale = [nt for t, nt in tier_tags.items() if t != tier and has_tag(item, nt[1], nt[0])]
                tag = tier_tags[tier]
                if stale or not has_tag(item, tag[1], tag[0]):
                    action = (tag, stale, tag[0])
        if price is not None and has_tag(item, no_sales_tag[1], no_sales_tag[0]):
            # carte vendue depuis : le tag "jamais vendu" n'a plus lieu d'être
            action = action or (None, [], "vendue")
            action[1].append(no_sales_tag)

        label = action[2] if action else "rien"
        if action and args.apply:
            tag, to_remove = action[0], action[1]
            try:
                ok, err = wm.add_tag(uc_id, tag[1]) if tag else (True, "")
                for _n, tid in to_remove if ok else []:
                    ok, err = wm.remove_tag(uc_id, tid)
                    if not ok:
                        break
            except ServerError as e:
                ok, err = False, str(e)
            if not ok:
                print(f"[error] {r_code:<3} {str(price):>5}  {name}  (erreur tag : {err})")
                errors += 1
                if errors >= 3:
                    stopped = 3
                    break
                continue
        print(f"[{'tag' if action else 'keep':5}] {r_code:<3} {str(price):>5}  {name}  ({label})")
        if action:
            done.append((name, r_code, price, label))
        if errors == 0:
            new_since = c["obtained_at"]

    if args.apply and not args.since:
        save_since(new_since)

    n_seen = len(done)
    print(f"\n{n_seen} carte(s) taguée(s)" + ("" if args.apply else " (dry-run, rien modifié)")
          + (f", {errors} erreur(s) : reprise au prochain passage" if errors else ""))

    if args.notify and (done or errors):
        lines = []
        # paliers d'abord, du plus cher au moins cher, puis jamais vendu, puis discard
        order = {"jamais vendu": 1, "discard": 2, "vendue": 3}
        for name, r_code, price, label in sorted(done, key=lambda d: (order.get(d[3], 0), -(d[2] or 0))):
            icon = {"discard": "🗑️", "jamais vendu": "❔", "vendue": "✅"}.get(label, "💰")
            lines.append(f"{icon} {name} ({r_code}) : {price if price is not None else '-'} → {label}")
        if errors:
            lines.append(f"⚠️ {errors} carte(s) en erreur, reprises au prochain passage")
        top = max((t for t in tiers for _n, _r, p, lab in done if lab == tier_tags[t][0]), default=0)
        title = f"WikiMasters : {len(cards)} nouvelle(s) carte(s)" + ("" if args.apply else " (simulation)")
        notify(title, "\n".join(lines), priority=4 if top >= args.notify_high else 3,
               tags=["moneybag"] if top else ["black_joker"])
    return stopped


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rarities", default="C,PC,R", help="raretés traitées (défaut C,PC,R)")
    ap.add_argument("--threshold", type=float, default=14, help="discard si prix moyen <= seuil (défaut 14)")
    ap.add_argument("--tiers", default=DEFAULT_TIERS, help="paliers de prix (défaut 75 puis x2 jusqu'à 9600)")
    ap.add_argument("--max-price", type=float, default=10000,
                    help="au-delà, prix jugé aberrant : aucun tag de prix (défaut 10000, 0 = pas de plafond)")
    ap.add_argument("--discard-tag", default="discard", help="nom du tag discard")
    ap.add_argument("--no-sales-tag", default="jamais vendu", help="nom du tag des cartes sans vente")
    ap.add_argument("--include-shiny", action="store_true", help="ne protège pas les shiny du discard")
    ap.add_argument("--since", help="traite les cartes obtenues après cette date ISO 8601 (sans toucher à l'état)")
    ap.add_argument("--first-lookback", type=float, default=24,
                    help="sans état précédent, remonte ce nombre d'heures (défaut 24)")
    ap.add_argument("--notify", action="store_true", help="envoie le résumé sur ntfy / Discord")
    ap.add_argument("--notify-high", type=float, default=1200,
                    help="priorité haute ntfy si une carte atteint ce palier (défaut 1200)")
    add_common_args(ap)
    args = ap.parse_args()
    try:
        code = run(args)
    except SystemExit as e:
        # arrêt fatal (session expirée, 401, 429...) : prévenir, sinon la surveillance s'arrête en silence
        if args.notify and e.code not in (0, None):
            notify("WikiMasters : surveillance arrêtée", str(e.code), priority=4, tags=["warning"])
        raise
    if args.notify and code == 2:
        notify("WikiMasters : session expirée",
               "Le renouvellement du token a échoué : recopie un cookie neuf (voir README).",
               priority=4, tags=["warning"])
    sys.exit(code)


if __name__ == "__main__":
    main()
