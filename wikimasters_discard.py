#!/usr/bin/env python3
"""
WikiMasters : pose le tag "discard" sur les cartes dont le prix moyen du marché
est inférieur ou égal à un seuil (par défaut 14, donc moins de 15).

Par défaut en DRY-RUN : rien n'est modifié, un CSV est produit dans exports/.
Les favoris, les shiny et les cartes en cours d'échange ne sont jamais tagués.
Les cartes sans aucune vente ne sont jamais taguées "discard" : c'est le rôle
de wikimasters_no_sales.py, qui leur pose le tag "jamais vendu".

  python wikimasters_discard.py                          # dry-run, seuil 14, raretés C,PC,R
  python wikimasters_discard.py --threshold 3            # autre seuil
  python wikimasters_discard.py --rarities R --max-cards 0  # toutes les rares
  python wikimasters_discard.py --apply --limit 1        # test réel sur 1 carte
  python wikimasters_discard.py --apply --auto-refresh   # vrai passage, token renouvelé tout seul

Voir README.md pour la configuration (.env).
"""
import argparse
import sys

from wikimasters_common import (ServerError, add_common_args, card_name, connect, has_tag,
                                open_price_cache, write_csv)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--threshold", type=float, default=14, help="tag si prix moyen <= seuil (défaut 14)")
    ap.add_argument("--rarities", default="C,PC,R", help="raretés ciblées (défaut C,PC,R)")
    ap.add_argument("--tag", default="discard", help="nom du tag (défaut discard)")
    ap.add_argument("--include-shiny", action="store_true", help="ne protège pas les shiny")
    ap.add_argument("--max-cards", type=int, default=50,
                    help="nombre max de cartes dont le prix est demandé au serveur par exécution ; "
                         "les cartes servies par le cache ne comptent pas (défaut 50, 0 = illimité)")
    add_common_args(ap)
    args = ap.parse_args()

    wm = connect(args.delay, args.auto_refresh)
    tag_id = wm.tag_id(args.tag, create=args.create_tags, color="#9ca3af")
    print(f"Tag '{args.tag}' -> {tag_id}")
    print(f"Mode : {'APPLY' if args.apply else 'DRY-RUN'} | seuil <= {args.threshold:g} | raretés {args.rarities}\n")

    price_cache = open_price_cache(args.cache_ttl)
    rows, tagged, errors, fetched, from_cache = [], 0, 0, 0, 0
    capped = expiring = server_down = False
    # séries d'erreurs serveur consécutives, séparées : un prix réussi ne doit pas masquer des échecs de tag
    server_errors, tag_server_errors, price_errors = 0, 0, 0  # série prix / série tags / total prix

    for rarity in [r.strip() for r in args.rarities.split(",") if r.strip()]:
        if capped or expiring or server_down:
            break
        for item, pending in wm.collection(rarity):
            if not wm.ensure_token():
                expiring = True
                break
            card = item.get("card") or {}
            card_id = item.get("card_id") or card.get("id")
            uc_id = item["id"]
            r_code = card.get("rarity") or rarity
            shiny = bool(item.get("is_shiny"))

            summary, price_error = price_cache.get(card_id), None
            if summary is None:
                # le plafond ne compte que les vraies requêtes de prix, pas les cartes servies par le cache
                if args.max_cards and fetched >= args.max_cards:
                    capped = True
                    break
                try:
                    summary = wm.sales_summary(card_id)
                    price_cache.put(card_id, summary)
                    server_errors = 0
                except ServerError as e:
                    # pas mis en cache : la carte sera réessayée au prochain passage
                    summary, price_error = {}, str(e)
                    server_errors += 1
                    price_errors += 1
                fetched += 1
            else:
                from_cache += 1
            price = (summary.get(r_code) or {}).get("average")

            decision, reason = "keep", ""
            if price_error:
                # une erreur serveur n'est pas « aucune vente » : jamais tagué
                decision, reason = "error", f"erreur prix ({price_error})"
            elif has_tag(item, tag_id, args.tag):
                decision, reason = "skip", "déjà tagué"
            elif item.get("starred"):
                decision, reason = "skip", "favori"
            elif shiny and not args.include_shiny:
                decision, reason = "skip", "shiny"
            elif uc_id in pending or card_id in pending:
                decision, reason = "skip", "échange en cours"
            elif price is None:
                # pas de prix : géré par wikimasters_no_sales.py (tag "jamais vendu")
                decision, reason = "keep", "aucune vente"
            elif price <= args.threshold:
                decision, reason = "discard", f"{price} <= {args.threshold:g}"
            else:
                reason = f"{price} > {args.threshold:g}"

            if decision == "discard" and args.apply:
                if args.limit and tagged >= args.limit:
                    decision, reason = "keep", "limite atteinte"
                else:
                    try:
                        ok, err = wm.add_tag(uc_id, tag_id)
                    except ServerError as e:
                        # surcharge / panne serveur : pas de nouvel essai, compte dans la série d'erreurs serveur
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
                "rarete": r_code, "shiny": shiny, "favori": bool(item.get("starred")),
                "exemplaires": item.get("count", 1), "prix_moyen": price,
                "decision": decision, "raison": reason,
            })
            print(f"[{decision:7}] {r_code:<3} {str(price):>5}  {name}  ({reason})")

            if errors >= 3:
                sys.exit("3 erreurs de pose de tag : arrêt. Vérifie le payload de user_card_tags.")
            if server_errors >= 3 or tag_server_errors >= 3:
                server_down = True
                break
        if wm.collection_error:
            print(f"Erreur serveur sur la page de collection : {wm.collection_error}")
            server_down = True

    out = write_csv(rows, "discard")

    n_discard = sum(r["decision"] == "discard" for r in rows)
    print(f"\n{len(rows)} cartes analysées, {fetched} articles interrogés, {from_cache} prix lus depuis le cache")
    if capped:
        print(f"Plafond --max-cards {args.max_cards} atteint : relance plus tard pour analyser la suite "
              f"(les cartes déjà vues seront lues depuis le cache).")
    print(f"{n_discard} à discard" + (f", {tagged} tags posés" if args.apply else " (dry-run, rien modifié)"))
    print(f"CSV : {out.relative_to(out.parent.parent)}")
    if price_errors:
        print(f"{price_errors} carte(s) en erreur prix (5xx/404/délai), non taguées : elles seront réessayées au prochain passage.")
    if server_down:
        print("Erreurs serveur ou réseau (5xx/404/délai dépassé) : arrêt propre. Réessaie plus tard.")
        sys.exit(3)
    if expiring:
        print("Token bientôt expiré (ou renouvellement refusé) : arrêt propre. "
              "Recopie un cookie neuf dans .env et relance (voir README.md).")
        sys.exit(2)


if __name__ == "__main__":
    main()
