#!/usr/bin/env python3
"""
WikiMasters : pose un tag de palier de prix sur les cartes dont le prix moyen
du marché atteint un seuil (par défaut 75 puis x2 jusqu'à 9600). Seul le palier
le plus haut atteint est gardé : une carte à 200 reçoit "150+", et un ancien tag
de palier ("75+") est retiré quand la carte change de palier.
Les autres tags ne sont jamais touchés.

Par défaut en DRY-RUN : rien n'est modifié, un CSV est produit dans exports/.

  python wikimasters_tiers.py --rarities R                         # dry-run, paliers 75 à 9600
  python wikimasters_tiers.py --rarities R --tiers 50,100,500      # autres paliers
  python wikimasters_tiers.py --rarities R --tag-format "val{seuil}"  # tags "val75", "val150"...
  python wikimasters_tiers.py --rarities C,PC,R --cache-only       # prix du cache seulement, aucune requête de prix
  python wikimasters_tiers.py --rarities R --apply --limit 1       # test réel sur 1 carte

Voir README.md pour la configuration (.env).
"""
import argparse
import sys

from wikimasters_common import (ServerError, add_common_args, card_name, connect, has_tag,
                                open_price_cache, write_csv)

# couleurs des tags créés par --create-tags, du palier le plus bas au plus haut
DEFAULT_TIERS = "75,150,300,600,1200,2400,4800,9600"
COLORS = ["#facc15", "#fb923c", "#ef4444", "#a855f7", "#6366f1", "#0ea5e9", "#14b8a6", "#22c55e"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rarities", required=True, help="raretés ciblées, ex. C,PC,R")
    ap.add_argument("--tiers", default=DEFAULT_TIERS,
                    help="paliers de prix (défaut 75 puis x2 jusqu'à 9600)")
    ap.add_argument("--max-price", type=float, default=10000,
                    help="au-delà de ce prix moyen, la carte est ignorée comme aberrante (défaut 10000, 0 = pas de plafond)")
    ap.add_argument("--tag-format", default="{seuil}+",
                    help="nom du tag pour un palier, {seuil} remplacé par sa valeur (défaut '{seuil}+')")
    ap.add_argument("--max-cards", type=int, default=50,
                    help="nombre max de cartes dont le prix est demandé au serveur par exécution ; "
                         "les cartes servies par le cache ne comptent pas (défaut 50, 0 = illimité)")
    ap.add_argument("--cache-only", action="store_true",
                    help="ne demande aucun prix au serveur : saute les cartes absentes du cache")
    add_common_args(ap)
    args = ap.parse_args()

    tiers = sorted({float(t) for t in args.tiers.split(",") if t.strip()}, reverse=True)
    if not tiers:
        sys.exit("--tiers vide.")

    wm = connect(args.delay, args.auto_refresh)

    # {seuil: (nom, id)}, du plus haut au plus bas
    tier_tags = {}
    for rank, t in enumerate(sorted(tiers)):
        name = args.tag_format.format(seuil=f"{t:g}")
        tier_tags[t] = (name, wm.tag_id(name, create=args.create_tags, color=COLORS[rank % len(COLORS)]))
    for t in tiers:
        print(f"Palier >= {t:g} -> tag '{tier_tags[t][0]}' ({tier_tags[t][1]})")
    print(f"Mode : {'APPLY' if args.apply else 'DRY-RUN'} | raretés {args.rarities}\n")

    price_cache = open_price_cache(args.cache_ttl)
    rows, tagged, errors, fetched, from_cache, not_cached, removed = [], 0, 0, 0, 0, 0, 0
    capped = expiring = server_down = False
    server_errors, tag_server_errors, price_errors = 0, 0, 0

    for rarity in [r.strip() for r in args.rarities.split(",") if r.strip()]:
        if capped or expiring or server_down:
            break
        for item, _pending in wm.collection(rarity):
            if not wm.ensure_token():
                expiring = True
                break
            card = item.get("card") or {}
            card_id = item.get("card_id") or card.get("id")
            uc_id = item["id"]
            r_code = card.get("rarity") or rarity

            summary, price_error = price_cache.get(card_id), None
            if summary is None and args.cache_only:
                not_cached += 1
                continue
            if summary is None:
                if args.max_cards and fetched >= args.max_cards:
                    capped = True
                    break
                try:
                    summary = wm.sales_summary(card_id)
                    price_cache.put(card_id, summary)
                    server_errors = 0
                except ServerError as e:
                    summary, price_error = {}, str(e)
                    server_errors += 1
                    price_errors += 1
                fetched += 1
            else:
                from_cache += 1
            price = (summary.get(r_code) or {}).get("average")

            tier = next((t for t in tiers if price is not None and price >= t), None)
            tag_name, tag_id = tier_tags[tier] if tier is not None else ("", None)
            need_add, stale = False, []

            decision, reason = "keep", ""
            if price_error:
                decision, reason = "error", f"erreur prix ({price_error})"
            elif price is None:
                reason = "aucune vente"
            elif args.max_price and price > args.max_price:
                # moyenne faussée par une vente aberrante : pas de palier
                decision, reason = "skip", f"prix aberrant ({price} > {args.max_price:g})"
            elif tier is None:
                reason = f"{price} < {tiers[-1]:g}"
            else:
                need_add = not has_tag(item, tag_id, tag_name)
                # anciens tags de palier de la carte, retirés pour ne garder que le bon
                stale = [(n, i) for t, (n, i) in tier_tags.items() if t != tier and has_tag(item, i, n)]
                if not need_add and not stale:
                    decision, reason = "skip", f"déjà tagué {tag_name}"
                else:
                    decision, reason = "tag", f"{price} >= {tier:g} -> {tag_name}"
                    if stale:
                        reason += f", retire {', '.join(n for n, _ in stale)}"

            if decision == "tag" and args.apply:
                if args.limit and tagged >= args.limit:
                    decision, reason = "keep", "limite atteinte"
                else:
                    try:
                        # pose avant retrait : en cas d'échec, la carte garde au moins un palier
                        ok, err = wm.add_tag(uc_id, tag_id) if need_add else (True, "")
                        for _n, stale_id in stale if ok else []:
                            ok, err = wm.remove_tag(uc_id, stale_id)
                            if not ok:
                                break
                            removed += 1
                    except ServerError as e:
                        tag_server_errors += 1
                        decision, reason = "error", f"erreur tag ({e})"
                    else:
                        if ok:
                            tagged += 1
                            tag_server_errors = 0
                        else:
                            errors += 1
                            decision, reason = "error", err

            name = card_name(card)
            rows.append({
                "user_card_id": uc_id, "card_id": card_id, "nom": name,
                "rarete": r_code, "shiny": bool(item.get("is_shiny")),
                "exemplaires": item.get("count", 1), "prix_moyen": price,
                "palier": tag_name, "decision": decision, "raison": reason,
            })
            print(f"[{decision:5}] {r_code:<3} {str(price):>7}  {name}  ({reason})")

            if errors >= 3:
                sys.exit("3 erreurs de modification de tag : arrêt. Vérifie les droits sur user_card_tags.")
            if server_errors >= 3 or tag_server_errors >= 3:
                server_down = True
                break
        if wm.collection_error:
            print(f"Erreur serveur sur la page de collection : {wm.collection_error}")
            server_down = True

    out = write_csv(rows, "tiers")

    n_tag = sum(r["decision"] == "tag" for r in rows)
    print(f"\n{len(rows)} cartes analysées, {fetched} articles interrogés, {from_cache} prix lus depuis le cache")
    if args.cache_only:
        print(f"{not_cached} cartes sautées car absentes du cache (--cache-only)")
    if capped:
        print(f"Plafond --max-cards {args.max_cards} atteint : relance plus tard pour analyser la suite.")
    print(f"{n_tag} à taguer" + (f", {tagged} cartes mises à jour, {removed} anciens paliers retirés"
                                 if args.apply else " (dry-run, rien modifié)"))
    print(f"CSV : {out.relative_to(out.parent.parent)}")
    if price_errors:
        print(f"{price_errors} carte(s) en erreur prix (5xx/404/délai), non taguées.")
    if server_down:
        print("Erreurs serveur ou réseau (5xx/404/délai dépassé) : arrêt propre. Réessaie plus tard.")
        sys.exit(3)
    if expiring:
        print("Token bientôt expiré (ou renouvellement refusé) : arrêt propre. "
              "Recopie un cookie neuf dans .env et relance (voir README.md).")
        sys.exit(2)


if __name__ == "__main__":
    main()
