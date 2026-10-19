"""
Code partagé par les scripts WikiMasters : configuration, session Supabase
(avec renouvellement du token), cache des prix et client HTTP du site.
"""
import base64
import contextlib
import csv
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import requests

if os.name == "nt":
    import msvcrt
else:
    import fcntl

BASE_DIR = Path(__file__).resolve().parent
EXPORT_DIR = BASE_DIR / "exports"
SITE = "https://www.wiki-masters.com"
PAGE_SIZE = 50
USER_AGENT = "wikimasters-tools/1.0"
CACHE_TTL = 24 * 3600  # secondes
# arrêt propre avant expiration du token : couvre les ~3 requêtes (2 s + 20 s de timeout) possibles entre deux vérifications
TOKEN_MARGIN = 90  # secondes
# avec --auto-refresh : renouvellement du token quand il reste moins de 5 min
REFRESH_MARGIN = 300  # secondes
SESSION_FILE = ".wm_session.json"  # session renouvelée, prioritaire sur le cookie de .env si plus récente
COOKIE_CHUNK = 3180  # taille max d'un morceau de cookie sb-...-auth-token, comme @supabase/ssr


# ---------------------------------------------------------------- système

def setup_console():
    """Évite un plantage à l'affichage d'un nom de carte que la console ne sait pas écrire (Windows)."""
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(errors="replace")


@contextlib.contextmanager
def file_lock(path: Path):
    """Verrou exclusif entre processus, bloquant, sous Linux, macOS et Windows."""
    with open(path, "a+") as f:
        if os.name == "nt":
            f.seek(0)
            while True:
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)  # réessaie 10 s puis lève OSError
                    break
                except OSError:
                    continue
            try:
                yield
            finally:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


def write_csv(rows, prefix):
    """Écrit le rapport d'un passage dans exports/ et renvoie son chemin."""
    EXPORT_DIR.mkdir(exist_ok=True)
    out = EXPORT_DIR / f"{prefix}_{datetime.now():%Y%m%d_%H%M%S}.csv"
    with out.open("w", newline="", encoding="utf-8-sig") as f:  # utf-8-sig : accents corrects dans Excel
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["vide"], delimiter=";")
        w.writeheader()
        w.writerows(rows)
    return out


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
        tmp.touch(mode=0o600)  # contient le refresh_token : lisible par le seul propriétaire (sauf Windows)
        tmp.write_text(json.dumps(self.session), encoding="utf-8")
        tmp.replace(self.path)

    def refresh(self):
        # verrou entre scripts lancés en parallèle : un seul renouvelle, les autres reprennent sa session,
        # sinon le second réutiliserait un refresh_token déjà consommé et Supabase couperait la session
        with file_lock(self.path.with_suffix(".lock")):
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
    """Cache persistant {card_id: {"fetched_at": epoch, "summary": {...}}}, entrées valides ttl secondes."""

    def __init__(self, path: Path, ttl: float = CACHE_TTL):
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


def open_price_cache(ttl_hours):
    cache = PriceCache(BASE_DIR / "price_cache.json", ttl=ttl_hours * 3600)
    print(f"Cache prix : {len(cache.data)} articles encore valides (< {ttl_hours:g} h)")
    return cache


# ---------------------------------------------------------------- client

class ServerError(Exception):
    """Erreur 5xx, 404 ou réseau : la carte est notée en erreur, sans nouvel essai."""


class WikiMasters:
    def __init__(self, auth: Auth, delay):
        self.auth, self.delay = auth, delay
        self.collection_error = None
        self.supabase_url = auth.supabase_url
        self._tags = None
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

    def _supa_call(self, method, table, what, **kw):
        """Requête REST Supabase ; une panne réseau à ce stade arrête le script proprement."""
        time.sleep(self.delay)
        try:
            resp = self.supa.request(method, f"{self.supabase_url}/rest/v1/{table}", timeout=20, **kw)
        except (requests.Timeout, requests.ConnectionError) as e:
            # rien n'a encore été modifié : arrêt propre plutôt qu'une trace d'erreur
            sys.exit(f"{type(e).__name__} en {what} sur Supabase : réseau ou serveur indisponible, réessaie plus tard.")
        r = self._check(resp)
        r.raise_for_status()
        return r.json()

    def tag_id(self, name, create=False, color=None):
        """Id du tag de ce nom ; le crée s'il manque et que create est vrai, sinon arrêt."""
        if self._tags is None:
            self._tags = self._supa_call("GET", "tags", "lisant les tags",
                                         params={"select": "*", "user_id": f"eq.{self.auth.user_id}"})
        for t in self._tags:
            if str(t.get("name") or "").lower() == name.lower():
                return t["id"]
        if not create:
            seen = ", ".join(sorted(str(t.get("name")) for t in self._tags)) or "aucun"
            sys.exit(f"Tag '{name}' introuvable. Crée-le dans le jeu ou relance avec --create-tags.\n"
                     f"Tags existants : {seen}")
        created = self._supa_call("POST", "tags", f"créant le tag {name}",
                                  json={"user_id": self.auth.user_id, "name": name, "color": color or "#9ca3af"},
                                  headers={"Prefer": "return=representation", "Content-Type": "application/json"})
        tag = created[0] if isinstance(created, list) else created
        self._tags.append(tag)
        print(f"Tag '{name}' créé.")
        return tag["id"]

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
    setup_console()
    load_env(BASE_DIR / ".env")
    cookie = os.environ.get("WM_COOKIE")
    apikey = os.environ.get("SUPABASE_APIKEY")
    if not cookie or not apikey:
        sys.exit("WM_COOKIE et SUPABASE_APIKEY doivent être définis dans .env (voir README.md).")
    auth = Auth(cookie, apikey, os.environ.get("SUPABASE_URL"), BASE_DIR / SESSION_FILE, auto_refresh)
    if not auth.ensure_fresh():
        if auto_refresh:
            sys.exit("Session expirée et renouvellement refusé : recopie un cookie neuf dans .env (voir README.md).")
        sys.exit("Le token du cookie expire dans moins de 2 min : recharge le site et recopie le cookie,\n"
                 "ou relance avec --auto-refresh.")
    print(f"Connecté : user {auth.user_id[:8]}..., token valide encore {auth.remaining // 60} min"
          + (" (renouvellement auto)" if auto_refresh else ""))
    return WikiMasters(auth, delay)


def card_name(card):
    return card.get("wikipedia_title") or card.get("title") or card.get("name") or card.get("label") or card.get("id", "?")


def add_common_args(ap):
    """Options communes aux deux scripts."""
    ap.add_argument("--apply", action="store_true", help="modifie réellement les tags (sinon dry-run)")
    ap.add_argument("--limit", type=int, default=0, help="nombre max de cartes modifiées (0 = illimité)")
    ap.add_argument("--create-tags", action="store_true", help="crée les tags manquants dans le jeu")
    ap.add_argument("--delay", type=float, default=2.0, help="pause entre requêtes en secondes (défaut 2)")
    ap.add_argument("--cache-ttl", type=float, default=CACHE_TTL / 3600,
                    help="durée de validité des prix en cache, en heures (défaut 24)")
    ap.add_argument("--auto-refresh", action="store_true",
                    help="renouvelle le token avant expiration (cookie d'une session réservée au script)")
