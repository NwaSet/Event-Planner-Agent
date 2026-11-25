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
- [x] Réutilisation des résultats validés, même si l'instruction est reformulée. Depuis les correctifs ciblés ci-dessous, une erreur réparable permet une seconde tentative lorsque le contexte pertinent change. Une sortie structurée incorrecte autorise une seule réparation dans la conversation du spécialiste.
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

Suite complète à l'issue de la simplification précédente : **245 tests réussis** avec :

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
- [ ] Vérifier le choix des filtres par le spécialiste, y compris lorsqu'une liste préparée n'inclut pas encore tous les critères. L'interprétation générale reste confiée aux agents ; seul le libellé explicite « soirée étudiante » / « événement étudiant » dispose désormais d'un garde-fou textuel ciblé.
- [ ] Mesurer les tokens et le nombre d'appels réels. Les 8 appels du scénario de référence sont une mesure simulée, pas une promesse de coût réel.
- [ ] Vérifier la qualité de la synthèse finale et l'exhaustivité des tâches annoncées. Sans liste `tasks`, la décision de finir appartient au Supervisor ; Python exige au moins une décision réussie pour déclarer un succès, mais ne déduit pas tout le périmètre à partir du texte.

Limites connues : une sélection par catégorie et un document par run ; pas de replanification automatique d'une décision validée. Les montants HT et TTC, déplacements, TVA, boissons et personnel ne constituent pas un devis final garanti. Les données ne permettent ni réservation ni confirmation de disponibilité. Aucun email n'est envoyé.

## Correctifs ciblés — trace étudiante du 24 novembre 2026

### Ce que les traces permettent d'établir

Dernière exécution enregistrée : `8fb23523`, de 21:54:03 à 21:54:34. Le journal contient 8 appels LLM : 7 du Supervisor et 1 de Decision-Making ; aucun appel Planning, Budgetary ou Writer enregistré. Ce fichier ne conserve que les statistiques et fins de génération, pas les arguments ni les observations. La reproduction des échanges se fonde donc sur les symptômes rapportés et le code, pas sur un transcript complet récupéré. Le journal existant est conservé intact.

La recherche avec `city=Namur` reproduit exactement les 7 salles, dont les 5 premières sont affichées par défaut. Toutes les 7 interdisent `soiree_etudiante` :

| Position catalogue | Identifiants | Soirée étudiante |
| --- | --- | --- |
| 1 à 5, affichées par défaut | `salle_010`, `salle_032`, `salle_054`, `salle_076`, `salle_098` | Interdite dans chacune |
| 6 et 7, masquées par la limite | `salle_120`, `salle_142` | Interdite dans les deux |

Il n'existe donc pas de solution étudiante cachée parmi les deux dernières. Les catalogues n'ont pas été modifiés pour fabriquer une solution.

### Causes et corrections implémentées

- **Préférences rejetées avant délégation.** Le contrat exigeait une chaîne alors que le Supervisor pouvait fournir un dictionnaire. Le format canonique transmis aux agents reste une chaîne. Un objet est converti en texte JSON compact, avec clés, valeurs imbriquées, listes et valeurs nulles préservées. Les prompts des cinq agents et le schéma de délégation décrivent ce format. Les souhaits qualitatifs restent séparés des contraintes obligatoires.
- **Échec réutilisé sans examiner les changements.** Le statut terminal empêchait tout nouvel essai. Un petit registre garde maintenant seulement le nombre d'essais et le dernier contexte utile par tâche, sans cache de résultats ni historique de versions. Deux tentatives maximum : une erreur réparable peut être retentée si les paramètres structurés ou une décision pertinente changent. Une reformulation seule ou des paramètres identiques ne relancent pas le spécialiste. Un résultat validé, un refus fournisseur et une limite atteinte restent terminaux. Les erreurs fournisseur sont considérées permanentes pour le run ; aucun refus n'est contourné.
- **Correction rendue impossible par l'arrêt anticipé.** Le Supervisor peut maintenant recevoir une erreur réparable et la corriger avant l'arrêt automatique, même si c'était la seule tâche annoncée. Il peut aussi conclure avec un bilan partiel si aucune correction utile n'est disponible.
- **Périmètre perdu après erreur locale.** La liste de tâches annoncée est conservée même si une exigence est incorrecte, pour permettre les délégations indépendantes.
- **Type étudiant absent des filtres.** Le prompt demande explicitement `event_type=soiree_etudiante`. Un garde-fou ciblé conserve aussi ce type lorsque la demande originale nomme explicitement une soirée/événement étudiant ; une soirée générique ou une formulation négative simple ne crée pas ce filtre. Les écritures équivalentes « Soirée étudiante » et `soiree_etudiante` sont normalisées. Le type est ensuite injecté dans les recherches de salles et traiteurs et ne peut pas être remplacé pour contourner une interdiction.
- **Liste préparée traitée comme exhaustive et déjà suffisante.** Les réponses de recherche exposent désormais les filtres appliqués, le caractère tronqué et l'ordre du catalogue, distinct d'un classement qualitatif. Le spécialiste doit compléter tout critère obligatoire manquant. Les contraintes de type, capacité, prix, horaires, exclusions et fournisseurs imposés sont vérifiées avant la limitation des résultats. Le classement qualitatif reste une décision IA.
- **Ancien candidat encore sélectionnable après correction de recherche.** La dernière recherche remplace la liste des candidats autorisés, au lieu de cumuler les anciens candidats potentiellement incompatibles. Une recherche sans résultat ne permet pas de revenir silencieusement à une salle interdite.
- **Diagnostic et poursuite.** Sans salle compatible, le bilan indique les filtres obligatoires en échec. Le traiteur et le prestataire peuvent toujours être recherchés indépendamment ; leur compatibilité avec une future salle et leur disponibilité restent à confirmer. Les contraintes obligatoires déjà enregistrées restent verrouillées : une réparation n'autorise pas à augmenter arbitrairement un plafond ou à remplacer le type demandé.

### Vérifications simulées

Suite finale : **259 tests réussis**, dont les **245 tests préexistants conservés** et **14 nouveaux cas de régression**, sans réseau ni appel LLM payant.

Commande : `.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider` (exécution autorisée hors des restrictions de dossiers temporaires du bac à sable).

Nouveaux cas : dictionnaire de préférences sans perte d'information et contexte des quatre spécialistes ; correction après erreur ; nouvelle tentative avec contexte modifié ; absence de répétition identique ou reformulée ; maximum de deux tentatives ; effectif ajouté pour réparer la recherche traiteur ; refus fournisseur permanent ; contrôle des 7 salles réelles de Namur ; type étudiant conservé malgré une délégation limitée à la ville ; écritures équivalentes du type ; soirée générique et négation simple ; filtre de toutes les contraintes avant `limit=1`, avec solutions placées après les six candidats interdits ; ancien candidat invalidé après recherche corrigée ; réparation avant arrêt du Supervisor ; réponse partielle avec traiteur et prestataire réellement enregistrés après échec Planning et absence de salle.

### Validation réelle encore ouverte

- [ ] Rejouer la demande étudiante initiale avec un LLM réel, après autorisation d'appel. Ces corrections sont validées par simulation, pas définitivement par le fournisseur.
- [ ] Vérifier la qualité d'extraction des capacités, budgets, horaires et exclusions dans des formulations variées. Le garde-fou textuel est volontairement limité ; il ne remplace pas une compréhension générale de la demande.
- [ ] Vérifier que le Supervisor utilise la tentative de correction utile et poursuit effectivement les services indépendants, sans épuiser ses tours en reformulations.
- [ ] Vérifier les choix qualitatifs lorsque plus de cinq candidats sont compatibles : l'ordre affiché reste celui du catalogue, avec recherche affinée ou limite portée à 10 si nécessaire.
- [ ] Obtenir une trace détaillée des arguments et observations lors du prochain essai si un incident persiste ; le journal de tokens seul ne permet pas d'établir le transcript exact.

Résultat attendu pour le catalogue actuel : **aucune salle étudiante à Namur**, diagnostic explicite et résultats indépendants disponibles. Une proposition complète avec une salle nécessite une modification autorisée du lieu demandé ou de nouvelles données réelles compatibles, pas un relâchement silencieux des contraintes.

## Corrections finales et reprise — 24 novembre 2026

Les modifications présentes au début de cette intervention ont été conservées, notamment les correctifs précédents non commités et les huit lignes ajoutées au journal par une exécution réelle. Aucun appel LLM payant n'a été effectué.

### Priorité 1 — calculs financiers terminés et testés

- Fonctions Python centralisées dans `model/finance.py`, avec `Decimal` et arrondi commercial au centime.
- Salle : location selon la période, nettoyage obligatoire, caution remboursable séparée des dépenses et somme à mobiliser incluant la caution. Si la période manque, le tarif journée est explicitement présenté comme hypothèse et le tarif soirée est également indiqué.
- Traiteur : formule × effectif, forfait livraison et kilomètres aller-retour facturés en supplément. Les boissons et le personnel supplémentaire restent à chiffrer.
- Prestataire : kit HT + déplacement par kilomètre et véhicule ; TVA calculée sur les postes HT connus uniquement lorsqu'un taux est fourni. Aucune TVA, distance ou quantité de véhicules n'est inventée. Un taux ou un trajet inconnu reste distinct de zéro.
- Les montants HT sans taux de TVA sont exclus du sous-total TTC. La caution reste hors dépense. Les sous-totaux ne sont pas présentés comme un devis complet.
- Les recherches compactes tiennent compte des frais et de la TVA calculables pour exclure les offres dépassant leur plafond. Les outils publics gardent leur contrat de repli.
- Les filtres effectivement utilisés pour une sélection validée sont conservés pour les calculs suivants.
- Dès qu'une sélection financière existe, la réponse affichée provient du bilan Python validé ; un texte final LLM ne peut pas remplacer les montants par une invention. Le Supervisor et les spécialistes continuent de prendre les décisions.

Fichiers modifiés pour cette priorité : `model/finance.py` (nouveau), `model/event_state.py`, `model/agents/search.py`, `model/event_orchestrator.py`, `tests/test_finance.py` (nouveau), `tests/test_orchestration.py`, `To Do.md`.

Validation : **271 tests réussis** avec `.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider`, sans réseau, exécutés hors des restrictions de fichiers temporaires du bac à sable. Couverture ajoutée : arrondis, nettoyage, caution, TVA, kilomètres × véhicules, inconnus distincts de zéro, dépassement du plafond avec frais et réponse LLM financièrement erronée remplacée par les valeurs Python.

État après cette étape : priorité 1 terminée en simulation ; aucune correction partielle laissée ouverte. Prochaine action précise : priorité 2, vérifier et normaliser les types des filtres des agents, puis protéger la distinction entre soirée musicale et concert, sans changer le repli des outils publics.

### Priorité 2 — filtres terminés et testés

- Normalisation uniquement à l'entrée des agents : entiers écrits en texte ou flottants entiers, nombres décimaux avec point/virgule, booléens textuels explicites et identifiant unique conservé comme chaîne. Les valeurs ambiguës, booléens utilisés comme nombres et identifiants numériques restent refusés.
- Les variantes d'enum, notamment `Soirée` → `soiree`, sont normalisées avant comparaison avec les contraintes enregistrées. Une représentation différente ne crée plus de faux conflit ; une valeur réellement différente ne relâche pas une contrainte obligatoire.
- Une soirée musicale/électronique ou avec DJ ne devient pas automatiquement un concert. Une catégorie `concert` non demandée est rejetée avec une indication de correction ; un concert explicitement demandé reste un filtre obligatoire.
- Les schémas/prompts précisent les types attendus. Les outils publics restent stricts et conservent le repli intégral existant.

Fichiers modifiés pour cette priorité : `model/agents/search.py`, `model/event_state.py`, `model/event_orchestrator.py`, `model/agents/prompt.py`, `tests/test_agent_filters.py` (nouveau), `tests/test_orchestration.py`, `To Do.md`.

Validation : **283 tests réussis** avec la même commande pytest sans réseau. Les 12 nouveaux cas couvrent les conversions, les valeurs ambiguës, les zéros initiaux, les contraintes verrouillées, la soirée musicale, sa négation de concert et le concert explicite. L'ancien test exigeant de rejeter le booléen textuel `false` vérifie désormais le rejet du texte ambigu `peut-être`.

État après cette étape : priorités 1 et 2 terminées en simulation ; aucune correction partielle. Prochaine action précise : priorité 3, rendre les horaires et les contraintes de salle lisibles dans le bilan Python, résoudre les noms des fournisseurs imposés et éviter une synthèse LLM devenue redondante.

## Reprise vérifiée — 25 novembre 2026

Avant toute modification, lecture de ce fichier et exécution de `git status`, `git diff`, `git diff --stat`, `git diff --check` et `git diff --cached`. Branche : `chore/getterTest`. Huit fichiers suivis étaient modifiés et trois nouveaux fichiers non suivis étaient présents (`model/finance.py`, `tests/test_finance.py`, `tests/test_agent_filters.py`). Aucun changement indexé ; aucune erreur signalée par `git diff --check`. Le journal contenait 21 lignes ajoutées non commitées, toutes préservées.

L'état réel était plus avancé que le point d'interruption décrit : les calculs financiers et les filtres étaient déjà implémentés et documentés. Aucune portion incomplète identifiée dans les filtres ; aucun correctif financier recommencé.

Vérification avant reprise de la présentation, sans réseau ni appel LLM payant :

- `.\.venv\Scripts\python.exe -m pytest tests/test_agent_filters.py tests/test_orchestration.py -q -p no:cacheprovider` : **114 tests réussis**.
- `.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider` : **283 tests réussis**, y compris les tests financiers (exécution autorisée hors du bac à sable pour les dossiers temporaires).

Priorité filtres confirmée terminée en simulation. Suite du travail : présentation finale uniquement ; conserver les calculs et les filtres validés.

### Priorité 3 — présentation terminée et testée le 25 novembre

- Dates et horaires affichés au format `19/12/2026 à 20:00`, avec la date de fin et la mention d'heure locale, y compris après minuit.
- Noms et localisations des salles, traiteurs et prestataires, noms de formules et descriptions de kits affichés. Les identifiants internes restent dans `event_data`.
- Contraintes de salle présentées en phrases : heure de fin de musique, noms des traiteurs et prestataires techniques autorisés. Une référence introuvable est signalée « à vérifier », sans nom inventé.
- Diagnostics de recherche lisibles (ville, type d'événement, effectif, etc.) au lieu d'un dictionnaire JSON. Les tâches non résolues utilisent des libellés français ; les documents et résultats partiels sont conservés. Sans salle, la compatibilité des services avec la future salle reste explicitement à confirmer.
- Le Supervisor affiche le bilan Python vérifié même lorsqu'il termine spontanément par une réponse textuelle ; cette réponse vérifiée est aussi utilisée dans sa trace finale. Les calculs financiers eux-mêmes n'ont pas été changés.
- Suppression de la réécriture LLM redondante du bilan du Supervisor. Les décisions et l'ordre des tâches restent pilotés par les agents IA ; les spécialistes autonomes conservent leur restitution habituelle. Le scénario Namur de référence passe de **8 à 7 appels LLM simulés**, toujours avec **4 délégations**.

Fichiers modifiés pendant cette reprise :

- `model/agent.py` : restitution directe du bilan vérifié, sans appel de synthèse supplémentaire.
- `model/event_state.py` : présentation des dates, noms, exclusivités et diagnostics.
- `tests/test_orchestration.py` : assertions adaptées au format lisible et au nouvel appel évité ; test de réponse spontanée et de trace finales vérifiées.
- `tests/test_presentation.py` (nouveau) : bilan complet lisible, référence fournisseur manquante, document et tâches non résolues dans une réponse partielle.
- `README.md` et `To Do.md` : fonctionnement et état de reprise actualisés.

Tests exécutés après correction :

- `.\.venv\Scripts\python.exe -m pytest tests/test_presentation.py tests/test_orchestration.py -q -p no:cacheprovider` : **105 tests réussis**, avant ajout du dernier cas sur la réponse spontanée et sa trace.
- `.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider` : **287 tests réussis** après ajout de ce cas ; aucun appel réseau/LLM payant. Les tests financiers et les tests de filtres passent sans nouvelle modification de leurs fichiers.
- `git diff --check` : aucune erreur. `git diff --cached` : toujours aucun changement indexé.
- Comparaison SHA-256 avec le début de la reprise : `model/finance.py`, `tests/test_finance.py`, `tests/test_agent_filters.py`, `model/agents/search.py`, les trois catalogues et `data/logs/logs.jsonl` sont inchangés.

Les échecs intermédiaires de tests portaient sur l'ancien format ISO, les identifiants affichés et le compteur de huit appels. Les assertions ont été remplacées par des contrôles explicites des dates lisibles, des noms exacts et des sept appels, en conservant les contrôles de décisions et de contraintes. Aucune régression fonctionnelle détectée par la suite finale.

### État prêt pour une prochaine reprise

- **Terminé en simulation :** calculs financiers, normalisation des filtres, distinction soirée musicale/concert, présentation finale et suppression de la synthèse redondante.
- **En cours / partiellement implémenté :** aucun correctif de cette série laissé en cours.
- **Fichiers nécessitant encore une modification :** aucun pour les trois priorités demandées. Toute nouvelle modification doit partir d'un échec reproduit ; ne pas écraser les changements non commités ni les nouveaux fichiers non suivis.
- **Encore ouvert :** validation avec un LLM réel, extraction de contraintes dans des formulations plus variées, exhaustivité des tâches annoncées, coûts et disponibilités à confirmer avec des données réelles. Les garde-fous textuels ciblés ne constituent pas une compréhension générale du langage naturel.
- **Prochaine action précise :** après autorisation explicite d'un essai LLM réel, rejouer la demande « soirée musicale à Namur, sans concert » puis le cas étudiant sans salle compatible, et conserver les arguments d'outils, observations et bilan final pour comparer aux scénarios simulés. Sans cette autorisation, ne lancer aucun appel réel. Si un échec apparaît, ajouter d'abord un test simulé dans `tests/test_agent_filters.py` ou `tests/test_orchestration.py`, puis corriger uniquement le composant concerné.

Avant de reprendre, relire cette section et réexécuter `git status`, `git diff`, `git diff --stat`, `git diff --check` et `git diff --cached`. Aucun commit ni reset n'a été effectué pendant cette reprise.

## Correction ciblée Decision-Making / devis — 25 novembre 2026

Demande reproduite : « Trouve une salle à Namur pour un anniversaire de 100 personnes et rédige un email pour demander un devis. Je veux valider le message avant toute utilisation. »

### Diagnostic et niveau de preuve

- Journal réel `data/logs/logs.jsonl`, demande `f57b6c71` : Decision-Making a reçu successivement `recursive/flint-v1.0:free` (1 563 tokens de sortie, `stop`), `liquid/lfm-3-1.7b:free` (8 063 tokens, `tool_calls`) et `nvidia/nemotron-3.5-super-vl:free` (928 tokens, `stop`). La route configurée était `openrouter/free`. Aucun de ces trois appels n'indique `length` ; les 8 063 tokens ne prouvent pas une troncature.
- **Limite historique :** ces entrées ne contiennent ni contenu brut, ni arguments/nombre des appels d'outils, ni décompte des tokens de raisonnement. Impossible de reconstruire la chaîne JSON rejetée, de déterminer si le texte était vide, ni d'attribuer les tokens au raisonnement. Le changement de modèle est observé ; son rôle causal dans l'échec reste non démontré.
- Cause confirmée par inspection et reproduction : le schéma de délégation ne décrivait que preferences, event_type, guests et budget_total. La ville dépendait entièrement de son extraction par le Supervisor ; Python lançait une recherche dès qu'un filtre quelconque existait, même incomplet.
- Cause confirmée : une liste de tâches limitée à venue pouvait terminer le run avant Writer. Le type email et la demande originale n'étaient pas garantis dans son contexte ; il n'existait pas de garde empêchant un email de devis sans salle sélectionnée.
- Cause confirmée : la réparation du JSON pouvait encore appeler des outils. Les interruptions `length` et les refus `content_filter` recevaient le même code permanent `llm_error`. Les résultats invalides n'étaient pas des succès enregistrés, mais leur diagnostic ne distinguait pas explicitement une sortie temporairement invalide d'une impossibilité métier.

### Corrections terminées et vérifiées en simulation

- `model/event_orchestrator.py` : schéma fondé sur les propriétés d'exigences existantes, notamment ville et contraintes obligatoires ; instructions explicites pour les tâches multiples et la délégation au Writer après sélection.
- `model/event_state.py` : garde ciblée conservant une ville du catalogue explicitement citée « à … » sans ambiguïté, un effectif littéral et le type anniversaire ; comparaison de ville insensible à la casse. Les critères structurés (budget, accessibilité, horaires, exclusions/fournisseurs) continuent à passer par les validations existantes, sans changement de l'architecture ni des calculs financiers.
- La demande explicite de rédaction d'email reste une tâche en attente même si tasks ne mentionne que venue. Writer reçoit la demande originale, le nom réel de la salle, les exigences et les coûts vérifiés. Le catalogue actuel ne contient pas de coordonnées email : le contexte interdit leur invention. L'email dépendant d'une salle reste bloqué si elle n'a pas été sélectionnée ; le bilan décrit ce blocage.
- `model/agents/prompt.py` : distinction entre appels d'outils natifs pendant la recherche et objet JSON compact en sortie finale. Avec des candidats couvrant les critères, choisir directement ; l'évaluation qualitative reste confiée au modèle.
- `model/agent.py` : pour une sélection disposant déjà de candidats, une seule réparation de format/interruption, sans outils ni restitution LLM supplémentaire après épuisement de cette réparation. Un appel d'outil reçu malgré cette interdiction est rejeté sans exécution. Les autres agents conservent leurs outils nécessaires (notamment ask_user).
- `model/llm.py` : `length` devient une sortie interrompue réparable distincte d'un refus fournisseur ; `content_filter` reste non réessayé. Journalisation du contenu brut retourné, des appels d'outils/arguments, des refus et du compteur de tokens de raisonnement lorsqu'il est fourni. Aucun message entrant ni contenu de raisonnement privé ajouté. Une métrique absente reste inconnue (`null`), jamais assimilée à zéro.
- Les sorties invalides/interrompues portent `error_kind=temporary_output`, les impossibilités métier un diagnostic distinct. Une répétition identique reste sans nouvel appel après la réparation déjà consommée ; ce n'est pas un succès ni une erreur définitivement réutilisée. Un contexte pertinent modifié permet une seconde délégation, avec au maximum deux délégations par tâche ; un succès reste conservé. Les limites globales restent inchangées.
- Les brouillons passent par ask_user, avec conservation exacte du texte approuvé ou refusé. Aucun outil d'envoi n'est exposé et aucun email n'a été envoyé.

### Tests exécutés

- `tests/test_orchestration.py` : 17 cas ajoutés (paramétrisations incluses). Ville et première recherche précise ; budget/accessibilité/période obligatoires ; quatre candidats compatibles sans recherche générale ; sorties vide/non JSON/JSON partiel ; interruption et réparation bornée ; rejet d'outils pendant la réparation ; erreur temporaire puis nouvelle tentative avec préférences modifiées ; conservation des succès ; diagnostics fournisseur ; sélection → Writer → ask_user, accord et refus ; blocage sans salle ; normalisation de la ville, types invalides et négation d'une rédaction ou d'une ville.
- Deux assertions existantes adaptées au comportement demandé : Planning reçoit désormais la ville explicite ; la sélection directe à Namur évite une recherche LLM et passe de quatre à trois appels simulés. Les autres tests existants sont conservés.
- `python -m pytest tests/test_orchestration.py tests/test_agent_filters.py -q -p no:cacheprovider` (interpréteur `.venv`) : **130 tests réussis**.
- `python -m pytest -q -p no:cacheprovider` (interpréteur `.venv`) : **304 tests réussis**, après les derniers ajustements d'effectif littéral, de diagnostic partiel et de validation du type de ville. Aucune régression détectée, notamment sur les finances et les filtres métier.
- `git diff --check` : aucune erreur de whitespace ; `git diff --cached --stat` : aucun changement indexé. Les avertissements LF/CRLF sont ceux de la configuration Git Windows.
- Un premier passage a rencontré l'erreur Windows d'accès au répertoire temporaire pytest, avant exécution du test de journalisation. Réexécution autorisée avec accès au dossier temporaire : réussite. Les fixtures bloquent le réseau ; **aucun appel LLM réel ou payant**.

### État de reprise et limites restantes

- **Terminé en simulation :** correctifs ci-dessus ; aucun correctif laissé partiellement implémenté. Fichiers de cette intervention : `model/event_orchestrator.py`, `model/event_state.py`, `model/agents/prompt.py`, `model/agent.py`, `model/llm.py`, `tests/test_orchestration.py`, `To Do.md`.
- Les changements préexistants, les catalogues, les fichiers financiers et la trace historique ont été conservés ; aucun reset, commit ou indexation effectué.
- **Non validé avec un véritable LLM :** respect effectif du format compact par les modèles gratuits successifs, qualité de la sélection et du brouillon, consommation réelle après correction. Les tests ne prouvent pas ces comportements fournisseur.
- Le garde-fou textuel reste volontairement ciblé. Formulations ambiguës, villes inconnues du catalogue, budgets et autres contraintes exprimés librement dépendent encore de leur extraction structurée par le Supervisor ; ils ne doivent pas être présentés comme une compréhension générale garantie. Aucune nouvelle refactorisation de compréhension du langage n'a été engagée.
- Disponibilités, coordonnées du destinataire et devis réel restent à confirmer. Les journaux anciens ne peuvent pas être enrichis rétroactivement avec les réponses perdues.
- **Prochaine action précise :** si un nouvel essai réel est explicitement autorisé, rejouer exactement la demande anniversaire/devis et conserver les nouvelles entrées avec `response`, `finish_reason`, `reasoning_tokens`, les observations et le bilan. Vérifier ville/type/effectif avant le premier choix, quatre candidats, au maximum une réparation de format, puis Writer et ask_user. En cas d'échec, reproduire d'abord le contenu observé dans `tests/test_orchestration.py` et corriger seulement `model/llm.py`, `model/agent.py` ou `model/event_state.py` selon la cause. Aucun fichier ne nécessite actuellement de modification supplémentaire pour les cas simulés.

## Audit AMMN — 25 novembre 2026 : écarts confirmés, non corrigés

Rapport créé : [AUDIT_AMMN.md](AUDIT_AMMN.md), conformité estimée **68/100** avec barème détaillé. Schéma PNG et fiche PDF retrouvés dans `../Projet`, effectivement lus (deux pages du PDF, rendu visuel vérifié). Audit documentaire uniquement : aucun fichier Python, catalogue ou journal modifié ; changements antérieurs conservés.

Cette section actualise l'état précédent : un nouvel essai réel enregistré par l'utilisateur révèle un problème Writer que les tests de succès simulé ne couvraient pas. Les 304 tests précédemment réussis ne prouvent pas une conformité complète au parcours humain annoncé.

- [ ] **P0 — Brouillon approuvé perdu après régénération.** Trace `52c0c76e` : ask_user reçoit un brouillon, puis Writer reformule après la validation rapportée par l'utilisateur. Le contrôle d'égalité rejette cette nouvelle version ; le document approuvé n'est pas enregistré. Cause confirmée par inspection ET replay isolé des deux réponses enregistrées avec approbation simulée : error/invalid_output, deux appels LLM simulés, documents vide. Correction minimale à faire dans `model/agent.py` et `model/event_state.py` : terminer dès ask_user et conserver exactement approval.content avec le booléen, sans nouvel appel LLM. Ajuster le prompt Writer et le test qui attend actuellement cette erreur ; ajouter le replay dans `tests/test_orchestration.py`.
- [ ] **P0 — Validation du plan/rapport global absente.** Le schéma prévoit l'accord sur le plan ; la fiche impose cet accord avant génération des emails. La validation du brouillon ne le remplace pas. Ajouter une barrière Python ask_user et un statut dans event_data, avant la délégation de rédaction d'email ; sur refus, conserver le plan et ne pas générer d'emails. Fichiers concernés : `model/event_state.py`, `model/event_orchestrator.py`, tests d'orchestration. Aucune mémoire persistante nécessaire.
- [ ] **P1 — Météo non transmise.** Planning possède get_meteo, mais sa sortie ne contient que start/end ; aucune observation météo partagée avec Decision-Making ou le bilan. Le schéma relie également Decision-Making à Open-Meteo, contrairement à la fiche qui place seulement cet outil chez Planning. Transmettre un résumé météo utile dans le run, préciser l'usage dans les prompts et gérer panne/hors horizon. Fichiers à examiner : `model/event_state.py`, `model/agents/prompt.py`, éventuellement `model/agents/agents.py` pour le lien direct, tests météo/orchestration. Disponibilité réelle de l'API non vérifiée pendant l'audit.
- [ ] **P1 — Documents multiples et statuts.** documents est remplacé par une liste d'un élément et un succès documents est réutilisé ; pas de validation distincte pour plusieurs emails/destinataires. Même cardinalité effective d'un seul prestataire technique. Compléter seulement les cas réellement demandés, sans réécrire l'architecture. Le bilan doit afficher l'accord global et l'accord positif du brouillon, pas seulement success ou « Document ».
- [ ] **P1 conditionnel — Révision après refus.** Les documents n'imposent pas une boucle de négociation ; un arrêt explicite suffit au correctif minimal. Si une révision est démontrée, invalider seulement décisions/accords affectés, car un résultat déjà validé n'est actuellement pas recalculé.
- [ ] **P2 — Consommation.** Réduire les schémas de délégation répétés et les demandes d'agents déjà terminées ; ne pas relever les limites. La synthèse Python et les calculs Python sont des différences d'implémentation justifiées, pas une disparition des agents IA. Writer n'est toutefois pas le rédacteur systématique du bilan standard annoncé dans les documents : l'expliciter ou lui confier un rapport expressément demandé sans refaire les montants.

Contrôles effectués pendant l'audit : 150 salles, 100 traiteurs, 40 prestataires ; identifiants uniques dans chaque catalogue ; aucune référence fournisseur de salle absente. Les journaux contiennent des appels des cinq agents. Un seul replay local ciblé du problème Writer, sans réseau ni nouveau fichier de test ; aucune suite complète relancée et aucun appel LLM réel/payant. Aucun de ces écarts n'est marqué corrigé.

**Prochaine action précise :** implémenter uniquement le P0 de conservation du brouillon dans Agent/EventRun ; rejouer le cas réel et tester accord, refus, texte exact, absence d'appel post-validation et absence d'envoi. Ensuite seulement, traiter le P0 d'approbation du plan avant emails. Relire l'audit et l'état Git avant modification ; ne pas écraser les changements non commités.
