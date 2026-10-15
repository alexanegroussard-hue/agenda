# Agenda Graphisme

Salons, expositions, forums et autres événements de graphisme et de design à Bruxelles et à Nantes.
La page affiche la semaine à venir, avec un onglet « Mois » et un bouton « Ajouter à Google Agenda » sur chaque événement.

## Fonctionnement

- `index.html` : la page. Elle lit `events.json`.
- `events.json` : les événements.
- `sources.json` : la liste des sites surveillés.
- `scripts/update.py` : lit les pages des sources, fait extraire les événements par un petit modèle libre (Ollama), contrôle chaque résultat avec du code et met à jour `events.json`.
- `.github/workflows/update.yml` : lance le script chaque dimanche sur GitHub Actions (gratuit pour un dépôt public).

Contrôles appliqués à chaque événement trouvé par le modèle : dates valides et dans la période (jusqu'à la fin du mois suivant), titre retrouvé dans le texte de la page, sujet lié au graphisme ou au design, horaires au bon format. Un événement qui échoue à un contrôle est écarté. Si rien n'est trouvé ou si plus de la moitié des sources échouent, `events.json` n'est pas modifié et l'exécution est marquée en échec (GitHub t'envoie un e-mail).

## Mise en route (une seule fois)

1. Dans le dépôt : **Settings → Pages**. Sous « Build and deployment », choisis **Deploy from a branch**, branche **main**, dossier **/ (root)**, puis **Save**. L'adresse de la page s'affiche au bout d'une minute ou deux.
2. Dans **Settings → Actions → General**, tout en bas, « Workflow permissions » : choisis **Read and write permissions** et enregistre.
3. Onglet **Actions** : ouvre « Mise à jour de l'agenda » et clique **Run workflow** pour tester sans attendre dimanche. La première exécution télécharge le modèle (environ 5 Go) et peut durer une à deux heures. Les suivantes réutilisent le modèle gardé en cache.
4. Quand l'exécution est terminée, ouvre le résumé de l'exécution : il indique, source par source, combien d'événements ont été retenus et pourquoi d'autres ont été écartés.

## Ajouter ou retirer un site

Modifie `sources.json` : une entrée par site, avec `id`, `name`, `city` (Bruxelles ou Nantes), `scale` (indicatif), `url` de la page qui liste les événements. `{mois}` et `{annee}` dans l'URL sont remplacés par le mois en français et l'année en cours. Deux réglages facultatifs : `"design_only": true` pour un lieu entièrement consacré au design (tous ses événements sont gardés, sans filtre de sujet) et `"max_chunks": 10` pour lire plus de texte sur une page très longue (4 par défaut, environ 7 000 caractères chacun). Le résumé de chaque exécution indique, par source, combien d'événements le modèle a extraits, combien ont été retenus, combien de texte a été lu et des exemples d'événements écartés comme hors sujet.

## Tester vite

Dans l'onglet **Actions → Mise à jour de l'agenda → Run workflow**, trois réglages :
- **only** : une ou plusieurs sources seulement (leurs `id`, séparés par des virgules). Un test sur une ou deux sources prend quelques minutes au lieu de plusieurs heures.
- **force** : relit toutes les pages. Sans cette case, une page identique à celle de la semaine précédente n'est pas relue par le modèle, ce qui économise beaucoup de temps. Les événements déjà trouvés restent dans `events.json`.
- **model** : `qwen2.5:7b` (plus fiable) ou `qwen2.5:3b` (environ deux fois plus rapide, un peu moins fiable).

Le résumé de chaque exécution donne la durée de lecture de chaque source, pour voir où passe le temps.

## Ajouter un événement à la main

Ajoute un objet dans `events.json` avec `"origin": "manual"` : le script ne le modifie ni ne le supprime. Champs : `id`, `title`, `city`, `category` (expo, salon, forum, conf, autre), `venue`, `address`, `start` et `end` (AAAA-MM-JJ), `days` (optionnel, jours d'ouverture, 0 = dimanche), `hours` (optionnel, `HH:MM-HH:MM`), `free`, `note`, `url`, `addedAt`.

## Limites connues

- Le modèle ne cherche pas de nouveaux sites : il lit seulement ceux de `sources.json`.
- Un petit modèle se trompe plus qu'un grand : relis de temps en temps les événements importants avant d'y aller, chaque événement a son lien source.
- Si un site change de mise en page, le modèle s'en sort en général, mais un site qui bloque les robots (`robots.txt`) ou qui charge son agenda par JavaScript n'est pas lisible. Il apparaît alors en échec dans le résumé.
- GitHub désactive les workflows programmés d'un dépôt public après 60 jours sans activité. Le workflow écrit chaque semaine un fichier `last-run.json` pour l'éviter. Si l'onglet Actions montre le workflow comme désactivé, clique **Enable workflow**.
