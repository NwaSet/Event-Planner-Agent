# Simplification de l'orchestration — 24 novembre 2026

Demande de référence conservée :
« Organise une soirée à Namur, sans limite de budget. Donne-moi une date, une salle, un traiteur et un prestataire. »

## Analyse préalable

- Architecture, outils, prompts, tests et modifications locales examinés, ainsi que l'historique Git récent (`17e6f1b`, `311335a`, `4beb2c6`). Les modifications locales préexistantes ont servi de base.
- `AUDIT_MULTI_AGENTS.md` est absent du dépôt et de son historique accessible sous ce nom. Son contenu n'a donc pas pu être consulté.
- Les trois catalogues JSON ont été consultés et laissés intacts : 150 salles, 100 traiteurs, 40 prestataires. Contraintes utiles : exclusivités par identifiants, capacité, équipements/logistique, délais, fin de musique et coûts par offre. Les catalogues ne donnent pas de disponibilités réelles.
- Le contenu précédent de ce fichier était uniquement la demande Namur ci-dessus, sans liste d'incidents détaillée.

## Changements réalisés

- [x] Suppression de l'outil `configure_event` et de son passage obligatoire avant délégation.
- [x] Suppression du brouillon de configuration, des tentatives et signatures de configuration, de l'état global « configuration échouée » et des adaptations de ses arguments.
- [x] Suppression du cache de délégations indexé par contexte et du calcul de dépendances avec propagation automatique des blocages.
- [x] Conservation du Supervisor LLM, des quatre spécialistes, de `Agent`, de ReAct et de `Agent.as_tool()` ; aucun workflow fixe ne remplace les choix IA.
- [x] Délégation directe avec une instruction ; `requirements` et `tasks` sont facultatifs. Une liste de tâches peut annoncer le périmètre sans fixer l'ordre.
- [x] Un `event_data` neuf par exécution. Python conserve la demande originale, les contraintes connues, les résultats compacts et les statuts. Aucun ancien événement ni historique de messages n'est rechargé.
- [x] Les agents reçoivent le texte original et le contexte utile à leur tâche. Les objets détaillés restent dans les catalogues locaux ; Decision-Making retourne uniquement les identifiants de la sélection et de son offre.
- [x] Une seule exécution par tâche après résultat terminal, même si l'instruction est reformulée. Les erreurs sont également mémorisées. Une sortie structurée incorrecte autorise une seule réparation dans la conversation du spécialiste.
- [x] Les limites globales d'appels et de délégations restent configurables. Les résultats acquis restent disponibles en cas de limite ou d'échec de synthèse.
- [x] Planning peut échouer sans empêcher salle, traiteur et prestataire. Les propositions restent conditionnelles à la disponibilité et, si nécessaire, aux horaires.
- [x] Une salle choisie avant Planning impose sa limite horaire ; une salle choisie après un fournisseur respecte la sélection déjà acquise.
- [x] Budgetary est ignoré sans plafond connu. Avec un plafond connu, les allocations contraignent les offres ; l'absence d'effectif pour calculer le traiteur ne bloque pas la salle.
- [x] Conservation des projections compactes, des schémas limités à la catégorie et de la déduplication des observations. Sans candidats préparés pertinents, le spécialiste recherche avec son outil avant de choisir.
- [x] Les recherches publiques conservent leur repli intégral ; celui-ci n'est pas injecté dans les conversations des agents.
- [x] Erreurs fournisseur/refus (`content_filter` compris) distinctes des erreurs de sortie JSON ; aucun essai de contournement. Un refus explicite arrête le spécialiste sans réparation.
- [x] Traces et statistiques de tokens conservées. Les modifications préexistantes de `data/logs/logs.jsonl` n'ont pas été modifiées par cette intervention.

## Tests exécutés sans appel payant

Tests progressifs : 61 tests d'orchestration après retrait de la configuration, puis 72 avec délégations directes et ordres inversés. Les anciens tests spécifiques au mécanisme supprimé ont été remplacés ; les validations métier et les assertions sur les sélections restent présentes.

Suite complète finale : **245 tests réussis** avec :

```powershell
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
```

Le réseau est bloqué par les fixtures. Les premiers lancements ont rencontré des restrictions d'accès aux dossiers temporaires de pytest, y compris dans le projet ; la suite complète a ensuite réussi avec l'exécution autorisée hors du bac à sable.

| Scénario | Vérification simulée |
| --- | --- |
| A — date à Namur | Planning direct sans `requirements` ni liste de tâches ; date future et horaires cohérents ; 3 appels LLM. |
| B — salle à Namur | Decision-Making direct, recherche `get_venues`, identifiant existant à Namur, sans Planning ; 4 appels LLM. |
| C — salle et traiteur | Deux délégations successives sans liste de tâches ; compatibilité et zone d'intervention contrôlées ; 5 appels LLM. |
| D — Namur complet | Date, salle, traiteur et prestataire réellement enregistrés, statut `success`, aucun Budgetary ni Writer ; **8 appels LLM et 4 délégations** par run, répété avec un état neuf. |
| E — échec Planning | Refus fournisseur : 1 tentative ; JSON incorrect : 2 tentatives. Les trois sélections aboutissent et sont restituées dans une réponse partielle. |
| F — contraintes | Traiteur/prestataire imposés, budget limité, offres exactes, délais, équipements et limite de musique respectés. Les trois postes connus sont contrôlés contre leur allocation. |

Autres contrôles : types exacts des identifiants, choix qualitatif du modèle conservé, répétitions sans nouvel appel spécialiste, plafonds globaux, échec de synthèse, isolation des diagnostics entre deux demandes, sortie prématurée sans décision refusée, validation du brouillon exact et refus d'email.

## À vérifier avec un LLM réel

- [ ] Rejouer A à F et la demande Namur après autorisation explicite d'appels payants. Les mocks valident les contrats et les garde-fous, pas la compréhension réelle du modèle.
- [ ] Vérifier que le Supervisor transmet correctement les contraintes explicites au moment utile, notamment le plafond, l'effectif, la période de location et la date imposée, sans remplir des champs inconnus.
- [ ] Vérifier le choix des filtres par le spécialiste lorsqu'aucun candidat n'est préparé. L'interprétation du texte reste une décision IA, pas un parseur déterministe Python.
- [ ] Mesurer les tokens et le nombre d'appels réels. Les 8 appels du scénario de référence sont une mesure simulée, pas une promesse de coût réel.
- [ ] Vérifier la qualité de la synthèse finale et l'exhaustivité des tâches annoncées. Sans liste `tasks`, la décision de finir appartient au Supervisor ; Python exige au moins une décision réussie pour déclarer un succès, mais ne déduit pas tout le périmètre à partir du texte.

Limites connues : une sélection par catégorie et un document par run ; pas de replanification automatique d'une tâche terminée. Les montants HT et TTC, déplacements, TVA, boissons et personnel ne constituent pas un devis final garanti. Les données ne permettent ni réservation ni confirmation de disponibilité. Aucun email n'est envoyé.
