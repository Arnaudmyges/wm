#!/usr/bin/env python3
"""
WikiMasters : pose le tag "jamais vendu" sur les cartes qui n'ont encore aucune
vente sur le marché pour leur rareté, et le retire dès qu'une vente apparaît.
Les autres tags ne sont jamais touchés.

Le tag est informatif : il est posé aussi sur les favoris, les shiny et les
cartes en cours d'échange.

Par défaut en DRY-RUN : rien n'est modifié, un CSV est produit dans exports/.

  python wikimasters_no_sales.py                           # dry-run, raretés C,PC,R
  python wikimasters_no_sales.py --rarities C,PC,R --cache-only  # prix du cache seulement
  python wikimasters_no_sales.py --tag "sans vente"        # autre nom de tag
  python wikimasters_no_sales.py --apply --limit 1         # test réel sur 1 carte

Voir README.md pour la configuration (.env).
"""
import argparse
import sys

from wikimasters_common import (ServerError, add_common_args, card_name, connect, has_tag,
                                open_price_cache, write_csv)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rarities", default="C,PC,R", help="raretés ciblées (défaut C,PC,R)")
    ap.add_argument("--tag", default="jamais vendu", help="nom du tag (défaut 'jamais vendu')")
    ap.add_argument("--max-cards", type=int, default=50,
                    help="nombre max de cartes dont le prix est demandé au serveur par exécution ; "
                         "les cartes servies par le cache ne comptent pas (défaut 50, 0 = illimité)")
    ap.add_argument("--cache-only", action="store_true",
                    help="ne demande aucun prix au serveur : saute les cartes absentes du cache")
    add_common_args(ap)
    args = ap.parse_args()

    wm = connect(args.delay, args.auto_refresh)
    tag_id = wm.tag_id(args.tag, create=args.create_tags, color="#f472b6")
    print(f"Tag '{args.tag}' -> {tag_id}")
    print(f"Mode : {'APPLY' if args.apply else 'DRY-RUN'} | raretés {args.rarities}\n")

    price_cache = open_price_cache(args.cache_ttl)
    rows, changed, errors, fetched, from_cache, not_cached, added, removed = [], 0, 0, 0, 0, 0, 0, 0
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
            tagged = has_tag(item, tag_id, args.tag)

            decision, reason = "keep", ""
            if price_error:
                # une erreur serveur n'est pas « aucune vente » : le tag n'est ni posé ni retiré
                decision, reason = "error", f"erreur prix ({price_error})"
            elif price is None:
                decision, reason = ("skip", "déjà tagué") if tagged else ("tag", "aucune vente")
            elif tagged:
                decision, reason = "untag", f"vendue depuis (prix moyen {price})"
            else:
                reason = f"prix moyen {price}"

            if decision in ("tag", "untag") and args.apply:
                if args.limit and changed >= args.limit:
                    decision, reason = "keep", "limite atteinte"
                else:
                    try:
                        ok, err = (wm.add_tag(uc_id, tag_id) if decision == "tag"
                                   else wm.remove_tag(uc_id, tag_id))
                    except ServerError as e:
                        tag_server_errors += 1
                        decision, reason = "error", f"erreur tag ({e})"
                    else:
                        if ok:
                            changed += 1
                            tag_server_errors = 0
                            if decision == "tag":
                                added += 1
                            else:
                                removed += 1
                        else:
                            errors += 1
                            decision, reason = "error", err

            name = card_name(card)
            rows.append({
                "user_card_id": uc_id, "card_id": card_id, "nom": name,
                "rarete": r_code, "shiny": bool(item.get("is_shiny")), "favori": bool(item.get("starred")),
                "exemplaires": item.get("count", 1), "prix_moyen": price,
                "decision": decision, "raison": reason,
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

    out = write_csv(rows, "no_sales")

    n_tag = sum(r["decision"] == "tag" for r in rows)
    n_untag = sum(r["decision"] == "untag" for r in rows)
    print(f"\n{len(rows)} cartes analysées, {fetched} articles interrogés, {from_cache} prix lus depuis le cache")
    if args.cache_only:
        print(f"{not_cached} cartes sautées car absentes du cache (--cache-only)")
    if capped:
        print(f"Plafond --max-cards {args.max_cards} atteint : relance plus tard pour analyser la suite.")
    print(f"{n_tag} à taguer '{args.tag}', {n_untag} à détaguer"
          + (f" ; {added} tags posés, {removed} retirés" if args.apply else " (dry-run, rien modifié)"))
    print(f"CSV : {out.relative_to(out.parent.parent)}")
    if price_errors:
        print(f"{price_errors} carte(s) en erreur prix (5xx/404/délai), non modifiées.")
    if server_down:
        print("Erreurs serveur ou réseau (5xx/404/délai dépassé) : arrêt propre. Réessaie plus tard.")
        sys.exit(3)
    if expiring:
        print("Token bientôt expiré (ou renouvellement refusé) : arrêt propre. "
              "Recopie un cookie neuf dans .env et relance (voir README.md).")
        sys.exit(2)


if __name__ == "__main__":
    main()
