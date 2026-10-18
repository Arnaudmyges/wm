#!/usr/bin/env python3
"""
WikiMasters : pose un tag de palier de prix sur les cartes dont le prix moyen
du marché atteint un seuil (par défaut 75 / 150 / 300). Seul le palier le plus
haut atteint est posé : une carte à 200 reçoit "150+", pas "75+".

Par défaut en DRY-RUN : rien n'est modifié, un CSV est produit.

  python wikimasters_tiers.py --rarities R,TR                      # dry-run, paliers 75,150,300
  python wikimasters_tiers.py --rarities R --tiers 50,100,500      # autres paliers
  python wikimasters_tiers.py --rarities R --tag-format "val{seuil}"  # tags "val75", "val150"...
  python wikimasters_tiers.py --rarities R --apply --limit 1       # test réel sur 1 carte

Les tags doivent exister dans le jeu avant le lancement.
Partage price_cache.json et .env avec wikimasters_discard.py : ne pas lancer
les deux scripts en même temps (le cache serait écrasé et la charge doublée).
"""
import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

from wikimasters_discard import CACHE_TTL, PriceCache, ServerError, card_name, connect, has_tag


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="pose réellement les tags (sinon dry-run)")
    ap.add_argument("--rarities", required=True, help="raretés ciblées, ex. R,TR")
    ap.add_argument("--tiers", default="75,150,300", help="paliers de prix (défaut 75,150,300)")
    ap.add_argument("--max-price", type=float, default=10000,
                    help="au-delà de ce prix moyen, la carte est ignorée comme aberrante (défaut 10000, 0 = pas de plafond)")
    ap.add_argument("--tag-format", default="{seuil}+",
                    help="nom du tag pour un palier, {seuil} remplacé par sa valeur (défaut '{seuil}+')")
    ap.add_argument("--limit", type=int, default=0, help="nombre max de tags posés (0 = illimité)")
    ap.add_argument("--max-cards", type=int, default=50,
                    help="nombre max de cartes dont le prix est demandé au serveur par exécution ; "
                         "les cartes servies par le cache ne comptent pas (défaut 50, 0 = illimité)")
    ap.add_argument("--cache-only", action="store_true",
                    help="ne demande aucun prix au serveur : saute les cartes absentes du cache "
                         "(sans écrire le cache, donc compatible avec un discard qui tourne en même temps)")
    ap.add_argument("--delay", type=float, default=2.0, help="pause entre requêtes en secondes (défaut 2)")
    ap.add_argument("--cache-ttl", type=float, default=CACHE_TTL / 3600,
                    help="durée de validité des prix en cache, en heures (défaut 24)")
    ap.add_argument("--auto-refresh", action="store_true",
                    help="renouvelle le token avant expiration (cookie d'une session réservée au script)")
    args = ap.parse_args()

    tiers = sorted({float(t) for t in args.tiers.split(",") if t.strip()}, reverse=True)
    if not tiers:
        sys.exit("--tiers vide.")

    wm = connect(args.delay, args.auto_refresh)

    # {seuil: (nom, id)}, du plus haut au plus bas ; find_tag_id s'arrête si un tag manque
    tier_tags = {}
    for t in tiers:
        name = args.tag_format.format(seuil=f"{t:g}")
        tier_tags[t] = (name, wm.find_tag_id(wm.auth.user_id, name))
        print(f"Palier >= {t:g} -> tag '{name}' ({tier_tags[t][1]})")
    print(f"Mode : {'APPLY' if args.apply else 'DRY-RUN'} | raretés {args.rarities}\n")

    price_cache = PriceCache(Path(__file__).with_name("price_cache.json"), ttl=args.cache_ttl * 3600)
    print(f"Cache prix : {len(price_cache.data)} articles encore valides (< {args.cache_ttl:g} h)")
    rows, tagged, errors, fetched, from_cache, not_cached = [], 0, 0, 0, 0, 0
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
            elif has_tag(item, tag_id, tag_name):
                decision, reason = "skip", f"déjà tagué {tag_name}"
            else:
                decision, reason = "tag", f"{price} >= {tier:g} -> {tag_name}"

            if decision == "tag" and args.apply:
                if args.limit and tagged >= args.limit:
                    decision, reason = "keep", "limite atteinte"
                else:
                    try:
                        ok, err = wm.add_tag(uc_id, tag_id)
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
                sys.exit("3 erreurs de pose de tag : arrêt. Vérifie le payload de user_card_tags.")
            if server_errors >= 3 or tag_server_errors >= 3:
                server_down = True
                break
        if wm.collection_error:
            print(f"Erreur serveur sur la page de collection : {wm.collection_error}")
            server_down = True

    out = Path(__file__).with_name(f"wikimasters_tiers_{datetime.now():%Y%m%d_%H%M%S}.csv")
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["vide"], delimiter=";")
        w.writeheader()
        w.writerows(rows)

    n_tag = sum(r["decision"] == "tag" for r in rows)
    print(f"\n{len(rows)} cartes analysées, {fetched} articles interrogés, {from_cache} prix lus depuis le cache")
    if args.cache_only:
        print(f"{not_cached} cartes sautées car absentes du cache (--cache-only)")
    if capped:
        print(f"Plafond --max-cards {args.max_cards} atteint : relance plus tard pour analyser la suite.")
    print(f"{n_tag} à taguer" + (f", {tagged} tags posés" if args.apply else " (dry-run, rien modifié)"))
    print(f"CSV : {out.name}")
    if price_errors:
        print(f"{price_errors} carte(s) en erreur prix (5xx/404/délai), non taguées.")
    if server_down:
        print("Erreurs serveur (5xx/404/délai dépassé) : arrêt propre. Réessaie plus tard.")
        sys.exit(3)
    if expiring:
        print("Token bientôt expiré (ou renouvellement refusé) : arrêt propre. "
              "Recharge le site, recopie le cookie dans .env et relance.")
        sys.exit(2)


if __name__ == "__main__":
    main()
