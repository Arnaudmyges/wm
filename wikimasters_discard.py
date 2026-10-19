#!/usr/bin/env python3
"""
WikiMasters : pose le tag "discard" sur les cartes communes / peu communes
dont le prix moyen du marché est sous un seuil.

Par défaut en DRY-RUN : rien n'est modifié, un CSV est produit.

  python wikimasters_discard.py                         # dry-run, seuil 3, raretés C,PC
  python wikimasters_discard.py --threshold 2           # autre seuil
  python wikimasters_discard.py --apply --limit 1       # test réel sur 1 carte
  python wikimasters_discard.py --apply                 # pose réellement les tags
  python wikimasters_discard.py --max-cards 0           # analyse toute la collection

  python wikimasters_discard.py --apply --auto-refresh  # renouvelle le token tout seul

Les prix sont mis en cache 24 h par card_id dans price_cache.json.
--auto-refresh renouvelle le token via le refresh_token de la session et garde
la session renouvelée dans .wm_session.json. Le cookie doit venir d'une session
réservée au script (fenêtre privée fermée sans se déconnecter) : un refresh_token
ne sert qu'une fois, et Supabase déconnecte tout si le navigateur réutilise l'ancien.
Config dans un fichier .env à côté du script (voir .env.example).
Dépendance : pip install requests
"""
import argparse
import base64
import csv
import fcntl
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import requests

SITE = "https://www.wiki-masters.com"
PAGE_SIZE = 50
USER_AGENT = "wikimasters-discard-perso/1.0"
CACHE_TTL = 24 * 3600  # secondes
# arrêt propre avant expiration du token : couvre les ~3 requêtes (2 s + 20 s de timeout) possibles entre deux vérifications
TOKEN_MARGIN = 90  # secondes
# avec --auto-refresh : renouvellement du token quand il reste moins de 5 min
REFRESH_MARGIN = 300  # secondes
SESSION_FILE = ".wm_session.json"  # session renouvelée, prioritaire sur le cookie de .env si plus récente
COOKIE_CHUNK = 3180  # taille max d'un morceau de cookie sb-...-auth-token, comme @supabase/ssr


# ---------------------------------------------------------------- config

def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def parse_supabase_cookie(cookie_str: str):
    """Reconstitue la session Supabase depuis le cookie sb-<ref>-auth-token(.0, .1...)."""
    cookies = {}
    for part in cookie_str.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            cookies[k] = v

    ref, chunks = None, {}
    for name, value in cookies.items():
        m = re.fullmatch(r"sb-([a-z0-9]+)-auth-token(?:\.(\d+))?", name)
        if m:
            ref = m.group(1)
            chunks[int(m.group(2) or 0)] = value
    if not chunks:
        sys.exit("Cookie Supabase sb-...-auth-token introuvable dans WM_COOKIE.")

    raw = "".join(chunks[i] for i in sorted(chunks))
    if raw.startswith("base64-"):
        raw = raw[len("base64-"):]
        raw += "=" * (-len(raw) % 4)
        raw = base64.urlsafe_b64decode(raw).decode("utf-8")
    else:
        raw = urllib.parse.unquote(raw)

    session = json.loads(raw)
    if isinstance(session, list):  # ancien format supabase-js
        session = {"access_token": session[0]}
    return ref, session


def jwt_payload(token: str) -> dict:
    part = token.split(".")[1]
    part += "=" * (-len(part) % 4)
    return json.loads(base64.urlsafe_b64decode(part))


class Auth:
    """Session Supabase du cookie ; avec auto_refresh, renouvelle le token via le refresh_token."""

    def __init__(self, cookie_str, apikey, supabase_url, session_path: Path, auto_refresh=False):
        self.ref, self.session = parse_supabase_cookie(cookie_str)
        self.cookie = cookie_str
        # cookies hors session Supabase, renvoyés tels quels après renouvellement
        self.others = [p.strip() for p in cookie_str.split(";") if "=" in p
                       and not re.fullmatch(r"sb-[a-z0-9]+-auth-token(?:\.\d+)?", p.split("=", 1)[0].strip())]
        self.supabase_url = (supabase_url or f"https://{self.ref}.supabase.co").rstrip("/")
        self.apikey, self.path, self.auto_refresh = apikey, session_path, auto_refresh
        if auto_refresh:
            # session renouvelée par un passage précédent : gardée sauf si le cookie de .env est plus récent
            self._adopt_saved()

    def _adopt_saved(self):
        """Reprend la session du fichier si son token est plus récent que le nôtre."""
        if not self.path.exists():
            return False
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            saved_exp = jwt_payload(saved["access_token"]).get("exp", 0)
        except (OSError, ValueError, KeyError, IndexError) as e:
            print(f"Session {self.path.name} illisible ({e}), ignorée.")
            return False
        if saved_exp <= self.exp:
            return False
        self.session = saved
        self.cookie = self._build_cookie()
        return True

    @property
    def token(self):
        return self.session["access_token"]

    @property
    def exp(self):
        return jwt_payload(self.token).get("exp", 0)

    @property
    def remaining(self):
        return int(self.exp - time.time())

    @property
    def user_id(self):
        return jwt_payload(self.token)["sub"]

    def _build_cookie(self):
        raw = json.dumps(self.session, separators=(",", ":")).encode("utf-8")
        value = "base64-" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        name = f"sb-{self.ref}-auth-token"
        chunks = [value[i:i + COOKIE_CHUNK] for i in range(0, len(value), COOKIE_CHUNK)]
        parts = ([f"{name}={chunks[0]}"] if len(chunks) == 1
                 else [f"{name}.{i}={c}" for i, c in enumerate(chunks)])
        return "; ".join(self.others + parts)

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.touch(mode=0o600)  # contient le refresh_token : lisible par le seul propriétaire
        tmp.write_text(json.dumps(self.session), encoding="utf-8")
        tmp.replace(self.path)

    def refresh(self):
        # verrou entre scripts lancés en parallèle : un seul renouvelle, les autres reprennent sa session,
        # sinon le second réutiliserait un refresh_token déjà consommé et Supabase couperait la session
        with open(self.path.with_suffix(".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self._adopt_saved() and self.remaining >= REFRESH_MARGIN:
                return True
            return self._refresh_locked()

    def _refresh_locked(self):
        try:
            r = requests.post(
                f"{self.supabase_url}/auth/v1/token", params={"grant_type": "refresh_token"},
                json={"refresh_token": self.session["refresh_token"]},
                headers={"apikey": self.apikey, "User-Agent": USER_AGENT}, timeout=20,
            )
        except requests.RequestException as e:
            print(f"Renouvellement du token impossible : {type(e).__name__}")
            return False
        if r.status_code != 200:
            # corps de réponse seulement : aucun jeton n'est affiché
            print(f"Renouvellement du token refusé : {r.status_code} {r.text[:200]!r}")
            return False
        session = r.json()
        session.setdefault("expires_at", int(time.time()) + int(session.get("expires_in", 3600)))
        self.session = session
        # sauvegarde immédiate : l'ancien refresh_token est désormais invalide
        self._save()
        self.cookie = self._build_cookie()
        return True

    def ensure_fresh(self):
        """True si le token reste utilisable, en le renouvelant avant expiration si auto_refresh."""
        if not self.auto_refresh:
            return self.remaining >= TOKEN_MARGIN
        return self.remaining >= REFRESH_MARGIN or self.refresh()


# ---------------------------------------------------------------- cache prix

class PriceCache:
    """Cache persistant {card_id: {"fetched_at": epoch, "summary": {...}}}, TTL 24 h."""

    def __init__(self, path: Path, ttl: int = CACHE_TTL):
        self.path, self.ttl = path, ttl
        self.data = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                print(f"Cache {path.name} illisible ({e}), ignoré.")
        now = time.time()
        self.data = {k: v for k, v in self.data.items()
                     if isinstance(v, dict) and now - v.get("fetched_at", 0) < ttl}

    def get(self, card_id):
        entry = self.data.get(str(card_id))
        return entry["summary"] if entry else None

    def put(self, card_id, summary):
        self.data[str(card_id)] = {"fetched_at": time.time(), "summary": summary}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)  # écriture atomique : le cache survit à un arrêt en cours de route


# ---------------------------------------------------------------- client

class ServerError(Exception):
    """Erreur 5xx ou 404 du serveur : la carte est notée en erreur, sans nouvel essai."""


class WikiMasters:
    def __init__(self, auth: Auth, delay):
        self.auth, self.delay = auth, delay
        self.collection_error = None
        self.supabase_url = auth.supabase_url
        self.site = requests.Session()
        self.site.headers.update({
            "Cookie": auth.cookie,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        })
        self.supa = requests.Session()
        self.supa.headers.update({
            "apikey": auth.apikey,
            "Authorization": f"Bearer {auth.token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        })

    def ensure_token(self):
        """True si le token reste utilisable ; le renouvelle si besoin et met à jour les en-têtes."""
        before = self.auth.token
        ok = self.auth.ensure_fresh()
        if self.auth.token != before:
            self.site.headers["Cookie"] = self.auth.cookie
            self.supa.headers["Authorization"] = f"Bearer {self.auth.token}"
            print(f"Token renouvelé, valide encore {self.auth.remaining // 60} min")
        return ok

    def _check(self, r: requests.Response) -> requests.Response:
        if r.status_code in (401, 403):
            # URL et corps de réponse seulement : aucun en-tête (cookie, apikey, token) n'est affiché
            sys.exit(f"{r.status_code} sur {r.request.method} {r.url}\n"
                     f"Réponse : {r.text[:200]!r}\n"
                     f"Vérifie le cookie, SUPABASE_APIKEY et SUPABASE_URL dans .env.")
        if r.status_code == 429:
            retry = r.headers.get("Retry-After", "non précisé")
            sys.exit(f"429 sur {r.url} : trop de requêtes (Retry-After : {retry}). Arrêt.")
        return r

    def _get_site(self, path, params=None):
        time.sleep(self.delay)
        try:
            resp = self.site.get(f"{SITE}{path}", params=params, timeout=20)
        except (requests.Timeout, requests.ConnectionError) as e:
            raise ServerError(f"{type(e).__name__} sur {SITE}{path}") from e
        r = self._check(resp)
        if r.status_code >= 500 or r.status_code == 404:
            raise ServerError(f"{r.status_code} sur {r.url}")
        r.raise_for_status()
        return r.json()

    def collection(self, rarity):
        """Itère sur toutes les pages de my-collection pour une rareté."""
        page = 0
        while True:
            try:
                data = self._get_site("/api/my-collection", {
                    "sort": "rarity", "rarity": rarity, "page": page, "stats": 0,
                })
            except ServerError as e:
                # fin d'itération propre : main() voit collection_error et s'arrête en écrivant le CSV
                self.collection_error = str(e)
                return
            items = data.get("collection") or []
            pending = set(data.get("pendingTradeCardIds") or [])
            for item in items:
                yield item, pending
            if len(items) < PAGE_SIZE:
                return
            page += 1

    def sales_summary(self, card_id):
        data = self._get_site(f"/api/marketplace/cards/{card_id}/sales", {"scope": "summary"})
        return (data or {}).get("summary") or {}

    def find_tag_id(self, user_id, tag_name):
        time.sleep(self.delay)
        try:
            resp = self.supa.get(
                f"{self.supabase_url}/rest/v1/tags",
                params={"select": "*", "user_id": f"eq.{user_id}"}, timeout=20,
            )
        except (requests.Timeout, requests.ConnectionError) as e:
            # rien n'a encore été fait : arrêt propre plutôt qu'une trace d'erreur
            sys.exit(f"{type(e).__name__} en lisant les tags sur Supabase : réseau ou serveur indisponible, réessaie plus tard.")
        r = self._check(resp)
        r.raise_for_status()
        tags = r.json()
        for t in tags:
            label = str(t.get("name") or t.get("label") or t.get("title") or "")
            if label.lower() == tag_name.lower():
                return t["id"]
        seen = [{k: t.get(k) for k in ("id", "name", "label")} for t in tags]
        sys.exit(f"Tag '{tag_name}' introuvable. Crée-le une fois dans le jeu. Tags vus : {seen}")

    def add_tag(self, user_card_id, tag_id):
        time.sleep(self.delay)
        try:
            resp = self.supa.post(
                f"{self.supabase_url}/rest/v1/user_card_tags",
                json={"user_card_id": user_card_id, "tag_id": tag_id},
                headers={"Prefer": "return=minimal", "Content-Type": "application/json"},
                timeout=20,
            )
        except (requests.Timeout, requests.ConnectionError) as e:
            raise ServerError(f"{type(e).__name__} sur POST user_card_tags") from e
        r = self._check(resp)
        if r.status_code >= 500:
            raise ServerError(f"{r.status_code} {r.text[:200]}")
        if r.status_code in (200, 201, 204, 409):  # 409 = déjà tagué
            return True, ""
        return False, f"{r.status_code} {r.text[:200]}"

    def remove_tag(self, user_card_id, tag_id):
        time.sleep(self.delay)
        try:
            resp = self.supa.delete(
                f"{self.supabase_url}/rest/v1/user_card_tags",
                params={"user_card_id": f"eq.{user_card_id}", "tag_id": f"eq.{tag_id}"},
                headers={"Prefer": "return=minimal"}, timeout=20,
            )
        except (requests.Timeout, requests.ConnectionError) as e:
            raise ServerError(f"{type(e).__name__} sur DELETE user_card_tags") from e
        r = self._check(resp)
        if r.status_code >= 500:
            raise ServerError(f"{r.status_code} {r.text[:200]}")
        if r.status_code in (200, 204):
            return True, ""
        return False, f"{r.status_code} {r.text[:200]}"


# ---------------------------------------------------------------- logique

def has_tag(item, tag_id, tag_name):
    for t in item.get("tags") or []:
        if isinstance(t, str) and t in (tag_id, tag_name):
            return True
        if isinstance(t, dict):
            values = {str(v) for v in t.values() if isinstance(v, (str, int))}
            nested = t.get("tag") if isinstance(t.get("tag"), dict) else {}
            values |= {str(v) for v in nested.values() if isinstance(v, (str, int))}
            if tag_id in values or tag_name in values:
                return True
    return False


def connect(delay, auto_refresh):
    """Lit .env, ouvre la session (renouvelée si besoin et permis) et renvoie le client."""
    load_env(Path(__file__).with_name(".env"))
    cookie = os.environ.get("WM_COOKIE")
    apikey = os.environ.get("SUPABASE_APIKEY")
    if not cookie or not apikey:
        sys.exit("WM_COOKIE et SUPABASE_APIKEY doivent être définis dans .env")
    auth = Auth(cookie, apikey, os.environ.get("SUPABASE_URL"),
                Path(__file__).with_name(SESSION_FILE), auto_refresh)
    if not auth.ensure_fresh():
        sys.exit("Le token du cookie expire dans moins de 2 min : recharge le site et recopie le cookie.")
    print(f"Connecté : user {auth.user_id[:8]}..., token valide encore {auth.remaining // 60} min"
          + (" (renouvellement auto)" if auto_refresh else ""))
    return WikiMasters(auth, delay)


def card_name(card):
    return card.get("wikipedia_title") or card.get("title") or card.get("name") or card.get("label") or card.get("id", "?")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="pose réellement les tags (sinon dry-run)")
    ap.add_argument("--threshold", type=float, default=3, help="tag si prix moyen <= seuil (défaut 3)")
    ap.add_argument("--rarities", default="C,PC", help="raretés ciblées (défaut C,PC)")
    ap.add_argument("--tag", default="discard", help="nom du tag (défaut discard)")
    ap.add_argument("--tag-no-sales", action="store_true", help="tague aussi les cartes sans aucune vente")
    ap.add_argument("--include-shiny", action="store_true", help="ne protège pas les shiny")
    ap.add_argument("--limit", type=int, default=0, help="nombre max de tags posés (0 = illimité)")
    ap.add_argument("--max-cards", type=int, default=50,
                    help="nombre max de cartes dont le prix est demandé au serveur par exécution ; "
                         "les cartes servies par le cache ne comptent pas (défaut 50, 0 = illimité)")
    ap.add_argument("--delay", type=float, default=2.0, help="pause entre requêtes en secondes (défaut 2)")
    ap.add_argument("--cache-ttl", type=float, default=CACHE_TTL / 3600,
                    help="durée de validité des prix en cache, en heures (défaut 24)")
    ap.add_argument("--auto-refresh", action="store_true",
                    help="renouvelle le token avant expiration (cookie d'une session réservée au script)")
    args = ap.parse_args()

    wm = connect(args.delay, args.auto_refresh)
    tag_id = wm.find_tag_id(wm.auth.user_id, args.tag)
    print(f"Tag '{args.tag}' -> {tag_id}")
    print(f"Mode : {'APPLY' if args.apply else 'DRY-RUN'} | seuil <= {args.threshold} | raretés {args.rarities}\n")

    price_cache = PriceCache(Path(__file__).with_name("price_cache.json"), ttl=args.cache_ttl * 3600)
    print(f"Cache prix : {len(price_cache.data)} articles encore valides (< {args.cache_ttl:g} h)")
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
                decision, reason = ("discard", "aucune vente") if args.tag_no_sales else ("keep", "aucune vente")
            elif price <= args.threshold:
                decision, reason = "discard", f"{price} <= {args.threshold}"
            else:
                reason = f"{price} > {args.threshold}"

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

    out = Path(__file__).with_name(f"wikimasters_{datetime.now():%Y%m%d_%H%M%S}.csv")
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["vide"], delimiter=";")
        w.writeheader()
        w.writerows(rows)

    n_discard = sum(r["decision"] == "discard" for r in rows)
    print(f"\n{len(rows)} cartes analysées, {fetched} articles interrogés, {from_cache} prix lus depuis le cache")
    if capped:
        print(f"Plafond --max-cards {args.max_cards} atteint : relance plus tard pour analyser la suite "
              f"(les cartes déjà vues seront lues depuis le cache).")
    print(f"{n_discard} à discard" + (f", {tagged} tags posés" if args.apply else " (dry-run, rien modifié)"))
    print(f"CSV : {out.name}")
    if price_errors:
        print(f"{price_errors} carte(s) en erreur prix (5xx/404/délai), non taguées : elles seront réessayées au prochain passage.")
    if server_down:
        print("Erreurs serveur (5xx/404/délai dépassé) : arrêt propre, le site ou Supabase semble saturé. Réessaie plus tard.")
        sys.exit(3)
    if expiring:
        print("Token bientôt expiré (ou renouvellement refusé) : arrêt propre. "
              "Recharge le site, recopie le cookie dans .env et relance.")
        sys.exit(2)


if __name__ == "__main__":
    main()
