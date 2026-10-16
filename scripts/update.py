#!/usr/bin/env python3
"""Met à jour events.json : lit les pages de sources.json, fait extraire les événements
par un petit modèle libre (Ollama), contrôle chaque résultat avec du code, fusionne.

Variables d'environnement utiles :
  MODEL         modèle Ollama (défaut : qwen2.5:7b)
  OLLAMA_URL    adresse d'Ollama (défaut : http://localhost:11434)
  TODAY         date du jour forcée, AAAA-MM-JJ (tests)
  FIXTURE_DIR   dossier de pages HTML locales <id>.html au lieu du réseau (tests)
  MOCK_LLM      fichier JSON de réponses simulées {id_source: {"events": [...]}} (tests)
  OUT_DIR       dossier d'écriture (défaut : racine du dépôt)
"""
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = Path(os.environ.get("OUT_DIR", ROOT))
MODEL = os.environ.get("MODEL", "qwen2.5:7b")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
UA = "GraphendaBot/1.0 (+https://github.com/alexanegroussard-hue/agenda)"
CHUNK_CHARS = 7000
MAX_CHUNKS = 4
KEEP_PAST_DAYS = 120
ARCHIVE_DAYS = 730
CITY_COUNTRY = {"Bruxelles": "Belgique", "Nantes": "France"}
ONLY = {x.strip() for x in os.environ.get("ONLY", "").split(",") if x.strip()}
FORCE = os.environ.get("FORCE", "").strip().lower() in ("1", "true", "yes")
MONTHS_FR = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
             "août", "septembre", "octobre", "novembre", "décembre"]

# Mots qui sauvent un événement que le modèle aurait jugé hors sujet (comparés au titre normalisé).
DESIGN_WORDS = re.compile(
    r"graphi|design|typo|affiche|poster|illustr|imprim|print|calligraph|livre d artiste|motion"
    r"|serigraph|gravure|lettrage|identite visuelle|bande dessinee|fanzine|\bzine|packaging"
)

SCHEMA = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "category": {"type": "string", "enum": ["expo", "festival", "salon", "forum", "conf", "autre"]},
                    "venue": {"type": "string"},
                    "city": {"type": "string"},
                    "address": {"type": "string"},
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                    "hours": {"type": "string"},
                    "url": {"type": "string"},
                    "related_to_graphic_design": {"type": "boolean"},
                },
                "required": ["title", "category", "start", "end", "related_to_graphic_design"],
            },
        }
    },
    "required": ["events"],
}

SYSTEM = (
    "Tu extrais des événements d'un texte de page web. Tu réponds uniquement par un objet JSON "
    "conforme au schéma. Tu n'inventes rien : tout ce que tu écris doit figurer dans le texte. "
    "Si une information est absente, tu écris une chaîne vide."
)


def today() -> dt.date:
    t = os.environ.get("TODAY")
    return dt.date.fromisoformat(t) if t else dt.date.today()


def window_end(d: dt.date) -> dt.date:
    """Dernier jour du mois suivant."""
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    y2, m2 = (y + 1, 1) if m == 12 else (y, m + 1)
    return dt.date(y2, m2, 1) - dt.timedelta(days=1)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def slug(s: str) -> str:
    return re.sub(r"\s+", "-", norm(s))[:60].strip("-")


# ---------------------------------------------------------------- réseau

_robots: dict = {}
_last_hit: dict = {}


def allowed_by_robots(url: str) -> bool:
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    if base not in _robots:
        rp = robotparser.RobotFileParser()
        try:
            r = requests.get(base + "/robots.txt", headers={"User-Agent": UA}, timeout=15)
            if r.status_code == 200:
                rp.parse(r.text.splitlines())
            else:
                rp.parse([])
        except requests.RequestException:
            rp.parse([])
        _robots[base] = rp
    return _robots[base].can_fetch(UA, url)


def fetch(source: dict, url: str) -> str:
    fixtures = os.environ.get("FIXTURE_DIR")
    if fixtures:
        return (Path(fixtures) / f"{source['id']}.html").read_text(encoding="utf-8")
    if not allowed_by_robots(url):
        raise RuntimeError("interdit par robots.txt")
    host = urlparse(url).netloc
    wait = 3 - (time.time() - _last_hit.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    _last_hit[host] = time.time()
    r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "fr,en;q=0.8"}, timeout=40)
    r.raise_for_status()
    return r.text


def page_text(html: str, base: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "svg", "iframe", "form"]):
        t.decompose()
    for a in soup.find_all("a", href=True):
        href = urljoin(base, a["href"])
        if href.startswith("http"):
            a.append(f" <{href}>")
    lines = [re.sub(r"\s+", " ", l).strip() for l in soup.get_text("\n").split("\n")]
    return "\n".join(l for l in lines if l)


def chunks(text: str, limit: int = MAX_CHUNKS):
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > CHUNK_CHARS and cur:
            out.append(cur)
            cur = ""
        cur += line[:CHUNK_CHARS] + "\n"
    if cur:
        out.append(cur)
    return out[:limit]


# ---------------------------------------------------------------- modèle

def scope_rule(source: dict) -> str:
    """Tous les événements de la page sont repris, quel que soit leur sujet. Le champ
    related_to_graphic_design sert seulement à repérer ceux qui relèvent du graphisme (voir « design »)."""
    return "- Liste TOUS les événements de la page, quel que soit leur sujet : n'en omets aucun.\n"


def build_prompt(source: dict, text: str, d: dt.date, end: dt.date) -> str:
    return (
        f"Date du jour : {d.isoformat()}. Source : {source['name']} (pays : {country_of(source)}).\n"
        f"Extrais les événements (expositions, salons, forums, conférences, festivals) qui se "
        f"déroulent entre le {d.isoformat()} et le {end.isoformat()}, même en partie.\n"
        "Règles :\n"
        "- Dates au format AAAA-MM-JJ. Si l'année n'est pas écrite, prends l'année de la date du jour. "
        "Pour un événement d'un seul jour, end = start.\n"
        "- category : \"festival\" seulement pour un événement de plusieurs jours qui regroupe plusieurs activités "
        "(expositions, conférences, ateliers, projections...) sous un même thème : festival, biennale, semaine du design. "
        "Une exposition seule n'est pas un festival. Sinon : expo, salon, forum, conf ou autre.\n"
        "- city : la ville où se déroule l'événement, telle qu'elle est écrite dans le texte, sinon chaîne vide.\n"
        "- hours : horaires d'ouverture au format HH:MM-HH:MM, sinon chaîne vide.\n"
        "- url : le lien de la page de l'événement s'il apparaît entre chevrons < > dans le texte, sinon chaîne vide.\n"
        + scope_rule(source) +
        "- related_to_graphic_design : true si l'événement concerne le graphisme, le design, la typographie, l'affiche, "
        "l'illustration, l'édition ou l'impression ; false sinon (photographie, peinture, histoire, musique...).\n"
        "- Si le texte ne contient aucun événement, renvoie {\"events\": []}.\n\n"
        f"TEXTE DE LA PAGE :\n{text}"
    )


def call_llm(source: dict, prompt: str) -> list:
    mock = os.environ.get("MOCK_LLM")
    if mock:
        data = json.loads(Path(mock).read_text(encoding="utf-8"))
        return data.get(source["id"], {}).get("events", [])
    r = requests.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": MODEL, "stream": False, "format": SCHEMA,
            "options": {"temperature": 0, "num_ctx": 8192},
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        },
        timeout=2400,
    )
    r.raise_for_status()
    return json.loads(r.json()["message"]["content"]).get("events", [])


# ---------------------------------------------------------------- contrôles

HOURS_RE = re.compile(r"^(\d{1,2})[:hH](\d{2})\s*[-–—]\s*(\d{1,2})[:hH](\d{2})$")


def parse_date(s):
    try:
        return dt.date.fromisoformat((s or "").strip())
    except ValueError:
        return None


def clean_event(raw: dict, source: dict, text_norm: str, d: dt.date, wend: dt.date):
    """Renvoie (événement, None) ou (None, motif du rejet)."""
    title = re.sub(r"\s+", " ", str(raw.get("title", ""))).strip()
    if not (3 <= len(title) <= 140):
        return None, "titre invalide"
    design = bool(raw.get("related_to_graphic_design") is True
                   or source.get("design_only")
                   or DESIGN_WORDS.search(norm(title)))
    start = parse_date(raw.get("start"))
    if not start:
        return None, "date de début invalide"
    end = parse_date(raw.get("end")) or start
    if end < start:
        return None, "fin avant début"
    if (end - start).days > 400:
        return None, "durée aberrante"
    if end < d or start > wend:
        return None, "hors période"
    words = [w for w in norm(title).split() if len(w) >= 3]
    if words and sum(w in text_norm for w in words) / len(words) < 0.6:
        return None, "titre absent de la page"
    hours = ""
    m = HOURS_RE.match(str(raw.get("hours", "")).strip())
    if m:
        h1, m1, h2, m2 = (int(x) for x in m.groups())
        if h1 < 24 and h2 < 24 and m1 < 60 and m2 < 60 and (h1, m1) < (h2, m2):
            hours = f"{h1:02d}:{m1:02d}-{h2:02d}:{m2:02d}"
    url = str(raw.get("url", "")).strip()
    if not url.startswith("https://"):
        url = source["_url"]
    category = raw.get("category") if raw.get("category") in ("expo", "festival", "salon", "forum", "conf", "autre") else "autre"
    city = re.sub(r"\s+", " ", str(raw.get("city") or "")).strip()[:60]
    if city and norm(city) not in text_norm:
        city = ""  # ville absente de la page : on ne la garde pas
    if category == "festival" and end == start:
        category = "autre"  # un festival dure au moins deux jours
    ev = {
        "title": title,
        "city": city or source.get("city", ""),
        "country": country_of(source),
        "category": category,
        "design": design,
        "venue": str(raw.get("venue") or source.get("venue") or "").strip()[:120],
        "address": str(raw.get("address", "")).strip()[:160],
        "start": start.isoformat(),
        "end": end.isoformat(),
        "free": False,
        "url": url,
        "source": source["id"],
        "origin": "auto",
    }
    if hours:
        ev["hours"] = hours
    return ev, None


def country_of(item: dict) -> str:
    return item.get("country") or CITY_COUNTRY.get(item.get("city", ""), "Autres")


def event_key(ev: dict) -> str:
    return f"{slug(ev['title'])}|{country_of(ev)}|{ev['start'][:4]}"


def richness(ev: dict) -> int:
    return sum(bool(ev.get(k)) for k in ("hours", "address", "venue")) + (ev.get("url", "") != "")


# ---------------------------------------------------------------- principal

_last_push = [0.0]


def write_progress(total: int, done: int, current: str, t_start: float, state: str = "running", push: bool = True) -> None:
    """Écrit progress.json (avancement approximatif) et, sur GitHub Actions, le publie au plus toutes les 4 minutes."""
    elapsed = time.time() - t_start
    remaining = int(elapsed / done * (total - done)) if done else None
    data = {"state": state, "total": total, "done": done, "current": current,
            "startedAt": dt.datetime.fromtimestamp(t_start, dt.timezone.utc).isoformat(timespec="seconds"),
            "updatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "elapsedSec": int(elapsed), "remainingSec": remaining}
    (OUT_DIR / "progress.json").write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")
    if not push or not os.environ.get("GITHUB_ACTIONS"):
        return
    if state == "running" and time.time() - _last_push[0] < 240 and done:
        return
    _last_push[0] = time.time()
    try:
        run = lambda *a: subprocess.run(a, cwd=OUT_DIR, check=True, capture_output=True, timeout=60)
        run("git", "config", "user.name", "agenda-bot")
        run("git", "config", "user.email", "agenda-bot@users.noreply.github.com")
        run("git", "add", "progress.json")
        run("git", "commit", "-m", f"Progression : {done}/{total}")
        run("git", "pull", "--rebase", "-X", "theirs", "origin", "main")
        run("git", "push")
    except Exception as e:  # la progression est facultative
        print(f"(progression non publiée : {e})")


def main() -> int:
    d, wend = today(), window_end(today())
    all_sources = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))["sources"]
    known_ids = {s["id"] for s in all_sources if s.get("kind") != "atelier"}
    sources = [s for s in all_sources if s.get("kind") != "atelier"]
    events_path = OUT_DIR / "events.json"
    existing = []
    if events_path.exists():
        existing = json.loads(events_path.read_text(encoding="utf-8")).get("events", [])

    # Événements supprimés depuis la page (deleted.json) : on ne les remet pas.
    deleted_ids, deleted_keys = set(), set()
    deleted_path = OUT_DIR / "deleted.json"
    if deleted_path.exists():
        for e in json.loads(deleted_path.read_text(encoding="utf-8")).get("events", []):
            if e.get("id"):
                deleted_ids.add(e["id"])
            if e.get("title") and e.get("start"):
                deleted_keys.add(event_key(e))

    def is_deleted(e: dict) -> bool:
        return e.get("id") in deleted_ids or (bool(e.get("title")) and bool(e.get("start")) and event_key(e) in deleted_keys)

    if ONLY:
        sources = [s for s in sources if s["id"] in ONLY]
        if not sources:
            print("Aucune source ne correspond à ONLY=" + ",".join(sorted(ONLY)))
            return 1
    cache_path = OUT_DIR / "page-cache.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    pending: dict = {}
    t_start = time.time()

    found: dict = {}
    report = []
    failed = skipped = 0
    rejected_total: dict = {}

    for si, s in enumerate(sources):
        write_progress(len(sources), si, s["id"], t_start)
        t0 = time.time()
        s = dict(s)
        s["_url"] = s["url"].replace("{mois}", MONTHS_FR[d.month - 1]).replace("{annee}", str(d.year))
        try:
            text = page_text(fetch(s, s["_url"]), s["_url"])
            if len(text) < 200:
                raise RuntimeError("page presque vide")
            digest = hashlib.sha256(f"{text}|{wend.isoformat()[:7]}|{MODEL}".encode()).hexdigest()
            if not FORCE and cache.get(s["id"]) == digest:
                skipped += 1
                report.append(f"- INCHANGÉE {s['id']} : page identique à la dernière lecture, non relue")
                continue
            text_norm = norm(text)
            accepted = returned = 0
            parts = chunks(text, int(s.get("max_chunks", MAX_CHUNKS)))
            read_chars = sum(len(p) for p in parts)
            rej_here: dict = {}
            samples: list = []
            for part in parts:
                for raw in call_llm(s, build_prompt(s, part, d, wend)):
                    returned += 1
                    ev, why = clean_event(raw, s, text_norm, d, wend)
                    if ev is None:
                        rejected_total[why] = rejected_total.get(why, 0) + 1
                        rej_here[why] = rej_here.get(why, 0) + 1
                        if why == "hors sujet" and len(samples) < 5:
                            samples.append(str(raw.get("title", ""))[:60])
                        continue
                    k = event_key(ev)
                    if k not in found or richness(ev) > richness(found[k]):
                        found[k] = ev
                    accepted += 1
            pending[s["id"]] = (digest, returned)
            line = (f"- OK {s['id']} : {accepted} retenu(s) sur {returned} extrait(s) par le modèle ; "
                    f"texte lu : {read_chars} sur {len(text)} caractères ; durée : {int(time.time() - t0)} s")
            if rej_here:
                line += " ; rejets : " + ", ".join(f"{k} ×{v}" for k, v in sorted(rej_here.items()))
            if samples:
                line += " ; exemples hors sujet : " + " / ".join(samples)
            report.append(line)
        except Exception as e:  # une source en panne ne bloque pas les autres
            failed += 1
            report.append(f"- ÉCHEC {s['id']} : {type(e).__name__}: {e}")

    ok_sources = len(sources) - failed
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    problems = []
    if not found and skipped == 0:
        problems.append("aucun événement retenu : events.json n'a pas été modifié")
    if failed > len(sources) / 2:
        problems.append(f"{failed} sources sur {len(sources)} en échec")

    # Une page n'est mémorisée comme « lue » que si le modèle a bien répondu (au moins une réponse non vide
    # dans cette exécution), pour qu'un modèle défaillant ne fasse pas sauter des pages la semaine suivante.
    model_ok = any(r > 0 for _, r in pending.values())
    for sid, (dg, r) in pending.items():
        if r > 0 or model_ok:
            cache[sid] = dg
    cache_path.write_text(json.dumps(cache, indent=0, sort_keys=True) + "\n", encoding="utf-8")

    if found:
        merged = {}
        expired = []
        for ev in existing:
            if ev.get("origin") != "manual" and ev.get("source") and ev["source"] not in known_ids:
                continue  # source retirée de la liste, ou devenue « atelier » : ses événements disparaissent
            if is_deleted(ev):
                continue
            ev.setdefault("country", country_of(ev))
            if ev.get("origin") == "manual":
                merged[f"manual|{ev.get('id', event_key(ev))}"] = ev
            elif parse_date(ev.get("end", "")) and parse_date(ev["end"]) >= d - dt.timedelta(days=KEEP_PAST_DAYS):
                merged[event_key(ev)] = ev
            elif parse_date(ev.get("end", "")):
                expired.append(ev)  # trop ancien pour l'agenda : passe dans archive.json
        added = updated = 0
        for k, ev in found.items():
            ev["id"] = f"{slug(ev['title'])}-{ev['start']}"
            if is_deleted(ev):
                continue
            old = merged.get(k)
            if old is None:
                ev["addedAt"] = d.isoformat()
                added += 1
            else:
                ev["addedAt"] = old.get("addedAt", d.isoformat())
                if {x: old.get(x) for x in ("start", "end", "hours", "venue", "address")} != \
                   {x: ev.get(x) for x in ("start", "end", "hours", "venue", "address")}:
                    updated += 1
            merged[k] = ev
        out = sorted(merged.values(), key=lambda e: (e["start"], e["title"]))
        events_path.write_text(
            json.dumps({"updated": stamp, "events": out}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        arch_path = OUT_DIR / "archive.json"
        arch = {}
        if arch_path.exists():
            for e in json.loads(arch_path.read_text(encoding="utf-8")).get("events", []):
                arch[e.get("id") or event_key(e)] = e
        for e in expired:
            arch[e.get("id") or event_key(e)] = e
        limit = d - dt.timedelta(days=ARCHIVE_DAYS)
        arch_out = sorted((e for e in arch.values() if (parse_date(e.get("end", "")) or d) >= limit and not is_deleted(e)),
                          key=lambda e: (e.get("end", ""), e.get("title", "")), reverse=True)
        arch_path.write_text(json.dumps({"events": arch_out}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        report.append(f"\nAjoutés : {added} · modifiés : {updated} · total dans le fichier : {len(out)}")
    write_progress(len(sources), len(sources), "", t_start, "done", push=False)
    (OUT_DIR / "last-run.json").write_text(
        json.dumps({"lastRun": stamp, "sourcesOk": ok_sources, "sourcesFailed": failed}) + "\n", encoding="utf-8")

    report.append(f"Sources relues : {len(sources) - failed - skipped} · inchangées : {skipped} · en échec : {failed} "
                  f"· durée du script : {int((time.time() - t_start) / 60)} min")
    if rejected_total:
        report.append("Rejets par les contrôles : " + ", ".join(f"{k} ×{v}" for k, v in sorted(rejected_total.items())))
    for p in problems:
        report.append(f"ATTENTION : {p}")
    text = f"## Mise à jour de Graphenda ({d.isoformat()})\n\n" + "\n".join(report) + "\n"
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
