#!/usr/bin/env python3
"""
Passage complet : discard, paliers de prix puis tag "jamais vendu" sur les
mêmes raretés, avec renouvellement auto du token. La mise en veille de
l'ordinateur est bloquée tant que ça tourne (Linux, macOS, Windows).

  python run_wikimasters.py                         # vrai passage : C,PC,R, discard < 15
  python run_wikimasters.py --dry-run               # simulation, rien n'est modifié
  python run_wikimasters.py --rarities R --threshold 5
  python run_wikimasters.py --only tiers            # paliers seulement

Les paliers et "jamais vendu" utilisent les prix que le discard vient de mettre
en cache : aucune requête de prix en double.
"""
import argparse
import ctypes
import os
import shutil
import subprocess
import sys
import time

from wikimasters_common import BASE_DIR, load_env, notify, setup_console

# boucle qui s'arrête quand le lanceur disparaît, même tué brutalement
WATCH_PARENT = ("import os,sys,time\np=int(sys.argv[1])\ntry:\n while True:\n  os.kill(p,0)\n  time.sleep(5)\n"
                "except OSError:\n pass")


class NoSleep:
    """Bloque la mise en veille pendant le bloc with, au mieux selon le système."""

    def __init__(self):
        self.proc = None

    def __enter__(self):
        ok = False
        if os.name == "nt":
            # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
            ok = bool(ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001))
        elif sys.platform == "darwin" and shutil.which("caffeinate"):
            # -i : pas de veille sur inactivité, -s : pas de veille sur secteur ; s'arrête avec ce processus
            self.proc = subprocess.Popen(["caffeinate", "-i", "-s", "-w", str(os.getpid())])
            ok = True
        elif shutil.which("systemd-inhibit"):
            self.proc = subprocess.Popen([
                "systemd-inhibit", "--what=sleep:handle-lid-switch", "--who=wikimasters",
                "--why=Passage WikiMasters en cours", "--mode=block",
                sys.executable, "-c", WATCH_PARENT, str(os.getpid()),
            ])
            time.sleep(1)
            ok = self.proc.poll() is None  # sorti tout de suite : verrou refusé
        print("Mise en veille bloquée pendant le passage." if ok else
              "Impossible de bloquer la mise en veille : garde l'ordinateur éveillé.")
        return self

    def __exit__(self, *exc):
        if os.name == "nt":
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


def run(script, *args):
    cmd = [sys.executable, "-u", str(BASE_DIR / script), *args]
    print(f"\n=== {script} {' '.join(args)}\n", flush=True)
    return subprocess.call(cmd)


def main():
    setup_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rarities", default="C,PC,R", help="raretés traitées (défaut C,PC,R)")
    ap.add_argument("--threshold", type=float, default=14, help="seuil discard : prix moyen <= seuil (défaut 14)")
    ap.add_argument("--dry-run", action="store_true", help="simulation : aucun tag modifié")
    ap.add_argument("--only", choices=["discard", "tiers", "no-sales"], help="ne lance qu'une des étapes")
    ap.add_argument("--create-tags", action="store_true", help="crée les tags manquants dans le jeu")
    ap.add_argument("--cache-ttl", type=float, default=24, help="validité des prix en cache, en heures (défaut 24)")
    ap.add_argument("--notify", action="store_true", help="prévient sur ntfy / Discord à la fin du passage")
    args = ap.parse_args()
    load_env(BASE_DIR / ".env")
    start = time.time()

    common = ["--rarities", args.rarities, "--auto-refresh", "--cache-ttl", f"{args.cache_ttl:g}"]
    if not args.dry_run:
        common.append("--apply")
    if args.create_tags:
        common.append("--create-tags")

    code = 0
    with NoSleep():
        if args.only != "tiers":
            code = run("wikimasters_discard.py", *common, "--threshold", f"{args.threshold:g}", "--max-cards", "0")
            # 0 = fini, 3 = arrêt sur erreurs serveur : les prix déjà en cache restent exploitables
            if code not in (0, 3):
                return code
        # après le discard, les prix sont en cache ; seuls, les prix manquants sont demandés au serveur
        extra = ["--cache-only"] if args.only is None else ["--max-cards", "0"]
        if args.only in (None, "tiers"):
            code = run("wikimasters_tiers.py", *common, *extra) or code
        if args.only in (None, "no-sales"):
            code = run("wikimasters_no_sales.py", *common, *extra) or code
    if args.notify:
        hours = (time.time() - start) / 3600
        if code == 0:
            notify("WikiMasters : passage complet terminé", f"Durée : {hours:.1f} h.", priority=2,
                   tags=["white_check_mark"])
        else:
            why = {2: "session expirée : recopie un cookie neuf (voir README)",
                   3: "erreurs serveur répétées : relancé au prochain passage"}.get(code, f"code {code}, voir le journal")
            notify("WikiMasters : passage complet arrêté", f"Après {hours:.1f} h, {why}.", priority=4, tags=["warning"])
    return code


if __name__ == "__main__":
    sys.exit(main())
