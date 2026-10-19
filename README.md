# WikiMasters : tri automatique de la collection

Scripts qui posent des tags sur les cartes de ta collection [WikiMasters](https://www.wiki-masters.com) selon leur prix moyen sur le marché :

- **`discard`** sur les cartes qui ne valent presque rien (par défaut : moins de 15), pour les recycler ;
- **paliers de prix** `75+`, `150+`, `300+`, `600+`, `1200+`, `2400+`, `4800+`, `9600+` sur les cartes qui valent cher. Une carte ne garde que son palier le plus haut : quand elle change de palier, l'ancien tag est retiré.

Ils ne font que poser et retirer ces tags. Ils ne vendent, ne recyclent et n'échangent jamais rien : c'est toi qui décides ensuite dans le jeu, en filtrant par tag.

> Projet perso, non officiel, sans lien avec WikiMasters. Il utilise ton compte avec ton propre cookie de connexion : à toi de voir si ça te convient. Les requêtes sont espacées de 2 s pour ne pas charger le site.

## Ce qui est protégé

- **Simulation par défaut** : sans `--apply`, rien n'est modifié, tu obtiens juste un rapport CSV dans `exports/`.
- Jamais de `discard` sur les **favoris**, les **shiny** ni les cartes **en cours d'échange**.
- Les cartes **sans aucune vente** ne sont pas taguées `discard` (option `--tag-no-sales` pour le faire).
- Un **prix moyen aberrant** (au-dessus de 10000, faussé par une vente bizarre) ne donne pas de palier.
- Une **erreur serveur** n'est jamais prise pour « pas de vente » : la carte est sautée et réessayée au passage suivant. Après 3 erreurs d'affilée, le script s'arrête proprement.
- Les autres tags de tes cartes ne sont jamais touchés.

## Installation

Il faut [Python](https://www.python.org/downloads/) 3.8 ou plus récent. Ça marche sous Linux, macOS et Windows.

```bash
git clone https://github.com/Arnaudmyges/wm.git
cd wm
python -m venv .venv
```

Puis active l'environnement et installe la dépendance :

| Système | Commande |
|---|---|
| Linux / macOS | `source .venv/bin/activate` |
| Windows (PowerShell) | `.venv\Scripts\Activate.ps1` |
| Windows (cmd) | `.venv\Scripts\activate.bat` |

```bash
pip install -r requirements.txt
```

Sous Windows, remplace `python` par `py` si la commande `python` n'existe pas.

## Configuration

Les scripts se connectent avec ta session du site. Il faut copier deux valeurs dans un fichier `.env` :

```bash
cp .env.example .env      # Windows : copy .env.example .env
```

### 1. Ouvre une session réservée au script

Le script renouvelle lui-même sa connexion toutes les heures. Pour ça, il ne doit **pas** partager la session de ton navigateur habituel, sinon le site vous déconnecte tous les deux.

1. Ouvre une **fenêtre de navigation privée**.
2. Connecte-toi sur <https://www.wiki-masters.com> et va dans ta collection.

### 2. Récupère le cookie

1. Dans cette fenêtre privée, ouvre les outils de développement (`F12`), onglet **Réseau** / **Network**.
2. Recharge la page (`F5`).
3. Clique sur une requête vers `www.wiki-masters.com`, par exemple `my-collection`.
4. Dans **En-têtes de requête** / **Request Headers**, copie toute la valeur de `Cookie`. Elle doit contenir `sb-...-auth-token`.
5. Colle-la dans `.env` entre les guillemets de `WM_COOKIE=""`.

### 3. Récupère la clé `apikey`

1. Toujours dans l'onglet Réseau, tape `supabase` dans le filtre.
2. Clique sur une requête vers `...supabase.co/rest/v1/...`.
3. Copie la valeur de l'en-tête `apikey`. C'est la clé publique du site, la même pour tout le monde.
4. Colle-la dans `.env` entre les guillemets de `SUPABASE_APIKEY=""`.

### 4. Ferme la fenêtre privée sans te déconnecter

Ferme simplement la fenêtre. **Ne clique pas sur « Déconnexion »** : ça invaliderait la session que le script vient de récupérer.

Ensuite, le script garde lui-même sa session à jour dans `.wm_session.json`. Tu n'auras à refaire ces étapes que si le site le déconnecte, par exemple après une longue période sans passage. Le script te le dit alors clairement.

> `.env` et `.wm_session.json` donnent accès à ton compte : ne les partage jamais. Ils sont exclus de Git par `.gitignore`.

### 5. Crée les tags

Les tags `discard` et `75+` à `9600+` doivent exister dans ton compte. Le plus simple : la première fois, ajoute `--create-tags` à la commande, et ils seront créés avec des couleurs déjà choisies. Tu peux aussi les créer à la main dans le jeu.

## Utilisation

### Le plus simple : tout d'un coup

```bash
python run_wikimasters.py --dry-run --create-tags   # 1re fois : simulation, crée les tags
python run_wikimasters.py                           # vrai passage
```

`run_wikimasters.py` lance le `discard` puis les paliers sur les raretés C, PC et R. Il renouvelle la connexion tout seul et empêche l'ordinateur de se mettre en veille pendant le passage.

Options utiles :

| Option | Effet |
|---|---|
| `--dry-run` | simulation, rien n'est modifié |
| `--rarities R` | raretés traitées (défaut `C,PC,R`) |
| `--threshold 5` | `discard` si le prix moyen est inférieur ou égal à 5 (défaut 14, donc moins de 15) |
| `--only discard` / `--only tiers` | une seule des deux étapes |
| `--cache-ttl 48` | réutilise les prix de moins de 48 h au lieu de 24 h |
| `--create-tags` | crée les tags manquants |

### Durée

Le premier passage demande le prix de chaque carte au site, à environ 500 cartes par heure : compte quelques heures pour une grosse collection. Les prix sont gardés 24 h dans `price_cache.json`, donc un nouveau passage le même jour va beaucoup plus vite.

Tu peux arrêter à tout moment avec `Ctrl+C` : tout ce qui a été fait reste fait, et le passage suivant reprend avec les prix déjà en cache.

### Ordinateur portable : capot fermé

Le lanceur bloque la mise en veille automatique, mais fermer le capot met quand même l'ordinateur en veille sur la plupart des systèmes, et le passage s'arrête alors. Garde le capot ouvert, ou change le réglage :

- **Linux** (systemd) : crée `/etc/systemd/logind.conf.d/lid.conf` avec
  ```ini
  [Login]
  HandleLidSwitchExternalPower=ignore
  ```
  puis `sudo systemctl kill -s HUP systemd-logind`. Ajoute `HandleLidSwitch=ignore` pour que ça marche aussi sur batterie.
- **Windows** : Panneau de configuration → Options d'alimentation → « Choisir l'action qui suit la fermeture du capot » → « Ne rien faire ».
- **macOS** : le Mac se met en veille capot fermé, sauf branché sur secteur avec un écran externe. Garde le capot ouvert.

### Scripts séparés

Pour un réglage plus fin, les deux scripts se lancent seuls. Ils sont en simulation tant que tu n'ajoutes pas `--apply`.

```bash
python wikimasters_discard.py --rarities C,PC --threshold 3 --max-cards 0
python wikimasters_discard.py --apply --limit 1                  # vrai test sur une seule carte
python wikimasters_tiers.py --rarities R --tiers 50,100,500      # autres paliers
python wikimasters_tiers.py --rarities C,PC,R --cache-only       # sans redemander les prix
```

`--help` sur chaque script liste toutes les options. Par défaut, chaque script ne demande que 50 prix au site par exécution (`--max-cards 0` pour tout faire), et s'arrête quand le jeton de connexion expire, au bout d'une heure au plus (`--auto-refresh` pour le renouveler).

## Rapports

Chaque passage écrit un CSV dans `exports/` (séparateur `;`, s'ouvre dans Excel ou LibreOffice) : une ligne par carte avec son prix moyen, la décision (`discard`, `tag`, `keep`, `skip`, `error`) et la raison.

## En cas de problème

| Message | Que faire |
|---|---|
| `WM_COOKIE et SUPABASE_APIKEY doivent être définis` | `.env` manquant ou vide : voir [Configuration](#configuration). |
| `401` / `403`, ou `renouvellement refusé` | Session expirée ou coupée : refais les étapes 1, 2 et 4 de la configuration. |
| `Tag '…' introuvable` | Relance avec `--create-tags`, ou crée le tag dans le jeu. |
| `429 … trop de requêtes` | Le site limite : attends un peu, ou augmente `--delay`. |
| `Erreurs serveur ou réseau … arrêt propre` | Site saturé ou connexion coupée : relance plus tard, le travail déjà fait est gardé. |

## Fichiers

| Fichier | Rôle |
|---|---|
| `run_wikimasters.py` | lanceur : `discard` puis paliers, anti-veille |
| `wikimasters_discard.py` | tag `discard` des cartes bon marché |
| `wikimasters_tiers.py` | tags de palier de prix |
| `wikimasters_common.py` | code partagé : connexion, cache des prix, appels au site |
| `.env` | ta configuration (secrète, non versionnée) |
| `.wm_session.json`, `price_cache.json`, `exports/` | créés par les scripts, non versionnés |
