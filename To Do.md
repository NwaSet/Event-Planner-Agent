# To Do — Event-Planner-Agent

## Afterwork : sélection et achèvement du parcours — 27 novembre 2026

Transcripts `48267a87`, `111ef5d1`, `bde56a22` et sauvegarde correspondante examinés ; arguments et état compact reproduits dans `tests/fixtures/afterwork_48267a87.json`. Quatre régressions initiales reproduites en échec avec modèles simulés : champ parasite `max_budget=null`, type afterwork sans équivalence, Budget en attente sans plafond et date passée acceptée. Un autre défaut de révision supprimait `date` lorsque `weekday` était transmis simultanément.

- [x] **Date :** le Supervisor reçoit `today`. « Premier samedi de décembre », le 27 novembre 2026, se résout au **5 décembre 2026**, sans appel LLM de réparation. Une année explicitement donnée reste respectée ; une date passée explicite est refusée. Une nouvelle date demandée ne se fait pas remplacer par une ancienne formulation relative. `date` et `weekday` peuvent être enregistrés ensemble.
- [x] **Filtres réellement bloquants :** aucun type `afterwork` n'existe littéralement dans les catalogues, contre 105 salles et 78 traiteurs adaptés à `reception_entreprise`. L'équivalence métier limitée `afterwork` / `after_work` → `reception_entreprise` est appliquée uniformément, y compris aux interdictions. Les 66 formules `cocktail` ont toutes `repas_complet=false` : l'intersection avec `full_meal=true` est vide. Les filtres de service sont désormais justifiés par les messages utilisateur, pas par les reformulations du modèle. « Préfère un bon repas » reste une préférence. Si cocktail et repas complet sont réellement imposés ensemble, le conflit reste un `no_match` ; aucun candidat n'est inventé.
- [x] **Géographie :** la fonction Overpass/Haversine existante est conservée. Les anciennes recherches n'atteignaient pas le classement géographique parce que les filtres métier éliminaient déjà tous les candidats, et le contrôle des services pouvait écarter toutes les salles. Les tests vérifient `nearest`, `province_fallback` et ville stricte avec des IDs réels du catalogue. Capacité, horaires, délais de réservation, budget et fournisseurs imposés restent contrôlés.

Audit du catalogue pour 100 personnes, le 5 décembre 2026 de 19 h 30 à 23 h 30 : 105 salles adaptées au type, 96 après capacité, 6 à Namur, puis **4 après horaires et faisabilité du traiteur**. Ce sont des compatibilités du catalogue fictif, pas des disponibilités confirmées.
- [x] **Tâches et révisions :** Budget est `skipped` sans plafond global, y compris après reprise ou annonce dans un lot. Les champs null sans effet, contraintes vides et doublons partagés n'entraînent plus de révisions artificielles. `revise_event` rend les valeurs effectivement appliquées au Supervisor. Le prompt demande ensuite l'exécution des tâches utiles. Aucune limite LLM n'est augmentée.
- [x] **Writer et résultat partiel :** l'email au traiteur reste en attente de sa sélection, puis de la validation du plan et du brouillon. La faute « pour le traiter » de la demande réelle est reconnue dans cette adresse explicite. Si aucune salle ne convient, les services peuvent être recherchés provisoirement depuis la ville ou province connue ; leur compatibilité avec la future salle reste à confirmer. L'email incomplet est bloqué explicitement sans boucle de réparation.
- [x] **Reprise réelle :** l'ancien afterwork et ses filtres de repas inventés sont corrigés à partir des messages humains sauvegardés ; les tâches concernées sont rouvertes sans perdre la demande d'email. Les sélections restent représentées par leurs IDs. Les documents et validations précédents sont conservés selon les règles existantes.
- [x] **43 cas ciblés distincts réussis**, sans suite complète : 19 cas afterwork, 8 Writer, 8 géographie et 8 cas existants d'orchestration/budget directement concernés. Parcours testé : clarification → date → salle → traiteur → prestataire optionnel → accord du plan → Writer → accord du brouillon, dans les limites existantes, avec reprise du même JSON. Aussi vérifiés : sauvegarde réelle en échec, révision volumineuse répétée sans effet, interdictions du catalogue, conflit de repas explicite et résultat partiel avec services indépendants. LLM et Overpass simulés, réseau interdit.

Catalogues et `model/geography.py` inchangés ; calculs financiers, cinq agents, ReAct et `Agent.as_tool()` conservés. Modifications Git antérieures préservées, sans modifier les conversations personnelles ni les logs. Aucun appel LLM réel ou payant ; qualité du comportement d'un modèle réel non mesurée.

## Writer après organisation — 27 novembre 2026

- [x] **Diagnostic de d4d7b578 :** « Génère le mail pour le traiteur » échappait au détecteur de demande. `supervisor_requirements` retirait alors `document_kind`, `revise_event` retournait `unchanged`, tandis que `remember(tasks=...)` créait malgré tout `documents=pending`. Le garde-fou Writer consultait le détecteur, pas ce statut : trois refus `not_requested`, puis épuisement des itérations. L'appel initial plaçait aussi `document_kind` hors de `requirements`, ce qui provoquait un `TypeError` reproduit avant correction.
- [x] **Action conversationnelle :** la demande explicite initialise `tasks.documents.document` (type, destinataire, objectif, demande originale), sans ajouter de contrainte métier. Les anciens arguments `document_kind` sont acceptés sans réparation LLM. Le Supervisor délègue une instruction courte ; Python fournit la sélection, l'offre, la salle, le planning et les coûts pertinents. Une instruction erronée de « confirmation de réservation » ne remplace pas la demande utilisateur ni le statut fictif des sélections.
- [x] **Parcours humain :** validation du plan, puis du brouillon exact ; clarification ciblée si destinataire ambigu ou prestataire absent. Les réponses oui/non ne régénèrent pas le texte. Un refus du plan reste enregistré et une nouvelle demande permet de le soumettre à nouveau. Les cinq agents, ReAct, `Agent.as_tool()` et les limites d'appels sont conservés.
- [x] **Documents multiples :** chaque résultat conserve son destinataire, objectif, texte et approbation dans la liste existante `results.documents`. Un nouveau document ne remplace pas les précédents et ne réutilise pas leur approbation. Le statut de conversation dépend de l'action actuelle, pas du refus historique d'un autre brouillon. Le même JSON conserve tous les documents après réouverture.
- [x] **Validation ciblée uniquement : 17 tests réussis**, LLM simulés et réseau interdit. Rejeu des arguments de la trace dans `tests/fixtures/writer_d4d7b578.json`, état réel `salle_076` / `traiteur_098` / `formule_04` / `005`, kit 2, 11 décembre 2026 ; deux emails successifs avec accord ou refus, persistance, invitation, clarification et absence de prestataire. Tests existants ciblés de validation exacte, refus du plan, restauration et Writer non sollicité également vérifiés. La suite complète n'a pas été lancée, conformément à la demande.

Les modifications préexistantes de `data/logs/logs.jsonl` sont conservées. Aucun catalogue ni conversation personnelle modifié, aucun appel LLM payant et aucun envoi d'email. La qualité rédactionnelle d'un LLM réel n'a pas été évaluée.

## Complément après le nouvel essai réel — 26 novembre 2026

La demande jointe a été renvoyée à l'identique, mais onze nouvelles entrées de journal décrivent un essai supplémentaire. Les traces `fcd52497`, `76ab8135` et `52923e36` ont été examinées sans modifier les logs ni les conversations. La sélection qualitative de salle fonctionne dans ce nouvel essai (`salle_054`). Deux causes distinctes du blocage restant ont été reproduites et corrigées.

- [x] **Nouvelle prestation et ancien budget :** après une allocation de 100 % à la salle, l'ajout du prestataire héritait d'un plafond nul et produisait `no_match` avant Decision-Making. La portée de chaque allocation est désormais conservée dans `event_data.budget_categories`. Une catégorie ajoutée hors de cette portée demande une révision à Budgetary avant la recherche. Le plafond global, le planning et les sélections encore compatibles restent conservés ; les coûts connus sont transmis à Budgetary. La répartition qualitative reste une décision IA.
- [x] **Compatibilité des sauvegardes :** faute de portée enregistrée, les catégories avec une allocation positive servent de référence. Lors d'une nouvelle demande portant sur une catégorie auparavant à zéro, Budgetary réexamine cette allocation une fois. Une allocation nouvelle qui couvre explicitement la catégorie, même avec une part nulle, est respectée et ne provoque pas de boucle. L'ouverture seule ne déclenche aucun recalcul.
- [x] **Lot d'outils incomplet :** Planning pouvait terminer le tour avant Budgetary, pourtant présent dans le même lot de `tool_calls`. Le Supervisor annonce désormais les tâches valides du lot avant le contrôle de fin. Il ne déduit aucune catégorie absente et n'ajoute pas de Writer non demandé. Les agents ordinaires conservent leur comportement ; ReAct et `as_tool()` restent utilisés. Clarification, validation en attente et refus humain arrêtent toujours immédiatement le traitement concerné.
- [x] **Régressions :** cinq cas d'ajout de prestation reproduits en échec avant correction, puis tests de reprise du même JSON, allocation révisée → véritable choix du spécialiste simulé, zéro explicite, absence de plafond global, absence de relance des succès et archivage exact des accords/documents de l'ancienne version. Le lot Planning → Budgetary → Decision-Making, les arguments invalides et les barrières humaines sont également couverts.

**Vérification finale : 21 tests supplémentaires, 305 tests ciblés puis 501 tests complets réussis**, réseau bloqué et aucun appel LLM réel ou payant. Commandes : `.\.venv\Scripts\python.exe -m pytest tests/test_delegation_batches.py tests/test_added_service_budget.py tests/test_decision_contract.py tests/test_constraint_revisions.py tests/test_dialogue.py tests/test_orchestration.py tests/test_conversations.py -q -p no:cacheprovider`, puis `.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider`. `git diff --check` sans erreur.

Les corrections précédentes, catalogues, conversations personnelles et nouvelles lignes de logs sont conservés. Aucun changement des formules financières, du DAO ou de l'interface. Restent à vérifier avec un véritable LLM la qualité de la nouvelle répartition, les sélections et le parcours complet ; aucun nouvel essai réel n'a été lancé par Codex. La récupération d'une ancienne allocation sans métadonnées dépend de la règle de compatibilité ci-dessus, pas d'une reconstruction des intentions depuis les logs.

Pendant la finalisation, un commit extérieur (`ebea8fe`) a sauvegardé les changements et intégré des ajouts de logs ainsi qu'une suppression de commentaire dans `model/agents/search.py`. Ils ont été conservés. La suite complète a été relancée sur cet état : **501 tests réussis**. Les empreintes des catalogues et conversations déjà présents au début restent identiques.

## Decision-Making et contraintes inventées — 26 novembre 2026

Trace réelle `4d4b9578` analysée et appels utiles copiés dans `tests/fixtures/decision_4d4b9578.json` (arguments d'outils uniquement, sans raisonnement ni journal technique). Le dépôt était propre au début de cette intervention. **Neuf cas de régression reproduits en échec avant correction.**

- [x] `category` était déjà dans les champs obligatoires du schéma. Ajouter une description explicite de `venue`, `catering`, `provider` et un contrôle avant la logique métier : catégorie absente, invalide ou ambiguë → `invalid_arguments`, aucun spécialiste exécuté, aucune catégorie déduite. `requirements` sous forme de chaîne est également rejeté.
- [x] Réduire les arguments exposés pour Decision-Making aux nouvelles contraintes métier pertinentes. Retirer les objets complets `venue_filters`, `catering_filters`, `provider_filters` des schémas du Supervisor. Planning, budget et exigences validées sont repris directement dans l'état Python. Les anciennes interfaces Python internes restent compatibles.
- [x] En mode conversationnel, ne pas importer les objets de filtres techniques renvoyés malgré le contrat. Les exigences PMR, places assises et type d'espace utilisent des champs métier plats, puis Python construit les filtres. Un ancien objet contenant une demande PMR/places assises explicite est traduit vers ces champs ; les valeurs sans appui dans le texte utilisateur sont écartées. Les contraintes déjà enregistrées restent disponibles sans devoir être répétées.
- [x] Ne pas accepter de TVA, distances ou véhicules arbitraires ; conserver les valeurs explicites, y compris un zéro réellement fourni. Ne pas fabriquer un effectif ou un budget à partir d'une valeur d'exemple. Les données inconnues restent absentes. Les filtres optionnels inventés par la recherche du spécialiste sont contrôlés également.
- [x] Séparer les préférences des obligations : « un samedi de préférence » alimente `preferences`, pas une restriction `weekday`. L'accessibilité obligatoire reste applicable même si une autre information est une préférence. Les valeurs inconnues `null` ne suppriment pas une contrainte connue ; un passage explicite au budget illimité reste possible.
- [x] Écarter `document_kind="email"` et une tâche `documents` sans demande de document ; refuser l'appel Writer non demandé avant exécution. Maintenir l'accord sur le plan puis la validation du brouillon pour un email explicitement demandé.
- [x] Conserver le diagnostic `no_match` et le registre des tentatives lors d'une révision sans critère utile : `replace=["venue"]` ne relance pas une recherche en échec inchangée. Une révision pertinente réouvre la tâche ; les choix Budgetary/Planning acquis restent conservés. Les limites existantes restent actives.
- [x] Candidats compatibles → sélection qualitative par le véritable circuit Agent/ReAct/`as_tool()`, simulé dans les tests. Aucun candidat compatible → `no_match` sans appel LLM Decision-Making, sans candidat fabriqué ni assouplissement des restrictions.

**Scénario vérifié :** « Organise un événement sur Namur. », puis « Étudiant, pour 100 personnes, maximum 10 000 €, un samedi de préférence. ». Clarification et reprise du même JSON, Budgetary et Planning réussis, appel sans catégorie rejeté, arguments pollués de la trace neutralisés, aucun email ajouté. À Namur strict, les salles du catalogue interdisent les soirées étudiantes : le résultat partiel est légitime même après suppression des filtres inventés. Un élargissement explicite à la province réautorise la recherche et un choix parmi les candidats compatibles. Un scénario anniversaire compatible confirme séparément l'appel qualitatif à Decision-Making.

**Vérification finale : 25 nouveaux cas, 284 tests ciblés puis 480 tests complets réussis**, réseau bloqué, aucun LLM réel ou payant. Le test historique du snapshot de prestataire a été adapté : recherche autonome du spécialiste avant le choix de B-Sound Events, kit 6, 3 000 EUR HT ; noms et montants restent consultables sans catalogue à la réouverture.

Commandes : `.\.venv\Scripts\python.exe -m pytest tests/test_decision_contract.py tests/test_dialogue.py tests/test_constraint_revisions.py tests/test_orchestration.py tests/test_conversations.py -q -p no:cacheprovider`, puis `.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider`. `git diff --check` sans erreur. Cinq agents, ReAct, `as_tool()`, DAO, interface et calculs financiers conservés ; catalogues, conversations sauvegardées et logs inchangés.

Restent ouverts :

- [ ] Refaire le parcours avec un véritable LLM : qualité des choix, extraction des formulations libres et respect du contrat par chaque fournisseur. Le schéma ne garantit pas qu'un modèle renseignera toujours `category` ; son omission est désormais contrôlée sans exécution métier.
- [ ] Formulations ambiguës, négations complexes et détails non reconnus : les contrôles ciblés ne remplacent pas les clarifications du Supervisor. Ils ne certifient pas une compréhension exhaustive du langage naturel.
- [ ] Les anciennes sauvegardes ne sont pas réécrites automatiquement : une contrainte polluée déjà enregistrée doit être explicitement corrigée. Aucun historique personnel n'a été modifié.

## Révisions des contraintes et reprise des spécialistes — 25 novembre 2026

Intervention ciblée à partir des traces `899ca971`, `4e72bf9f`, `559f5934` et de la conversation sauvegardée correspondante. Les logs conservent les arguments des outils ; la sauvegarde confirme le conflit `location_scope`, la salle en `no_match` et le budget global remplacé par le plafond demandé pour le prestataire.

- [x] Reproduire les causes avant correction : **10 échecs et 1 contrôle réussi** dans les nouveaux tests simulés. Les révisions étaient acceptées mais ne réouvraient pas les tâches sans sélection ; les plafonds de recherche devenaient persistants ; le montant local écrasait le budget global.
- [x] Garder `revise_event` comme voie explicite de modification des exigences existantes, avec validation et retour à l'état précédent en cas de conflit. Une délégation contradictoire répond désormais `requires_revision` sans remplacer les décisions ni ajouter une erreur durable à la tâche.
- [x] Séparer les filtres temporaires `max_budget`, `ids`, `limit` des exigences persistantes lors d'une délégation de sélection. Les essais restent locaux au tour ; `selection_filters` reste un relevé du choix effectué. Un filtre explicitement posé via `revise_event` reste persistant. Effectif, période et autres critères métier ne disparaissent pas lors de la reprise.
- [x] Réouvrir uniquement les échecs dont les données utiles changent (`error`, `no_match`, blocages et limites compris). Réinitialiser leur registre après une révision explicite ou une nouvelle décision dépendante ; conserver les succès compatibles et les échecs indépendants. Une révision sans changement ne réinitialise rien.
- [x] Conserver la limite de deux essais lors de simples corrections des filtres temporaires et les limites globales existantes. Une reformulation du message n'est pas un nouveau critère de recherche.
- [x] Protéger le budget global dans le cas observé « trouve le prestataire sur Namur pour 2500 euros » : le montant est un plafond local du prestataire, y compris si le modèle le transmet à tort comme `budget_total`. Aucun appel Budgetary supplémentaire pour cette demande locale. Une modification explicite du budget global et sa répétition inchangée restent acceptées. Le plafond local reste soumis à l'allocation globale existante ; il ne déclenche pas une redistribution implicite.
- [x] Tester la fermeture/réouverture du même JSON, la sélection après correction, l'absence de réexécution des autres spécialistes et les protections contre les répétitions. **23 nouveaux cas**, **221 tests ciblés puis 455 tests complets réussis**, réseau bloqué et aucun LLM réel ou payant.

Commandes : `.\.venv\Scripts\python.exe -m pytest tests/test_constraint_revisions.py tests/test_dialogue.py tests/test_orchestration.py -q -p no:cacheprovider`, puis `.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider`. `git diff --check` sans erreur. Code modifié uniquement dans `model/event_state.py`, tests ajoutés dans `tests/test_constraint_revisions.py`. Prompts, orchestration, DAO, interface, finances et catalogues non modifiés pendant cette intervention. Conversations et logs existants conservés. `requirements.txt` a changé pendant l'intervention sans écriture de Codex ; cette modification extérieure a été laissée intacte.

Restent ouverts :

- [ ] Vérification avec un véritable LLM des demandes libres, des révisions et des choix qualitatifs. Aucun essai réel effectué ici.
- [ ] Formulations ambiguës ou plusieurs montants/prestations : le garde-fou local couvre une demande visant le seul prestataire avec un montant explicite ; il ne remplace pas la clarification du Supervisor ni une compréhension générale du langage.
- [ ] Autres erreurs d'interprétation visibles dans les traces (par exemple catégorie générique `soirée`, services/documents ajoutés sans demande) : hors de cette correction des contraintes et réexécutions. Aucune optimisation de prompts ou de tokens réalisée.

## Conversation interactive — vérifiée en simulation le 25 novembre 2026

- [x] A : état réutilisé entre tours, ajout ciblé d'un traiteur, réponse directe du Supervisor et contexte borné (4 derniers messages, 1 200 caractères chacun). 175 tests dialogue/orchestration réussis.
- [x] B : poursuite du même JSON depuis le menu ou une ancienne sauvegarde ; ouverture seule sans agents, nouveaux champs initialisés à la prochaine interaction. 59 tests dialogue/menu/persistance réussis.
- [x] C : clarifications et validations plan/email sauvegardées dans `pending_input`, retour à l'interface sans saisie bloquante dans l'agent. Accord/refus du brouillon conservé exactement, zéro appel LLM après réponse oui/non. 236 tests ciblés réussis, anciens tests one-shot inclus.
- [x] D : modifications ciblées d'effectif, budget, salle, date et prestations ; conservation des décisions compatibles, réévaluation des autres et archivage des anciennes décisions/approbations. 252 tests ciblés réussis à cette étape.
- [x] E : revue des critères extraits par les spécialistes, parcours Rich de reprise, README et audit actualisés. Vérification finale : **255 tests ciblés puis 432 tests complets réussis**.

Chaque étape est vérifiée sans réseau ni appel LLM réel. Conversations personnelles, catalogues et modifications préexistantes de `requirements.txt` et des logs conservés.

Commandes finales : `.\.venv\Scripts\python.exe -m pytest tests/test_dialogue.py tests/test_orchestration.py tests/test_main.py tests/test_conversations.py -q -p no:cacheprovider`, puis `.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider`. Les tests utilisent leurs propres répertoires temporaires. Le lanceur Windows vers une nouvelle console Rich reste couvert par simulation.

Le contrôleur propose `run_conversation` et `continue_conversation`, sans saisie terminal. Un échange conserve le même identifiant et JSON ; l'ouverture seule reste sans exécution et sans écriture. Les anciens JSON version 1 reçoivent leurs champs manquants au prochain tour. Les limites LLM/délégations repartent à zéro par message. Le Supervisor reçoit seulement l'état utile et quatre échanges récents ; les archives des documents restent consultables dans la sauvegarde.

Les validations sont des attentes persistées, distinctes des clarifications. Accord sur le plan → Writer ; refus → aucun email généré. Le oui/non sur le brouillon conserve son texte exact, sans nouveau LLM. Une modification substantielle archive les accords/documents avec leur révision et exige un nouvel accord pour le plan actualisé. Aucun envoi d'email. Les cinq agents, ReAct, `as_tool()`, calculs financiers et entrée directe one-shot restent disponibles ; le terminal utilise désormais le mode conversationnel demandé.

**À tester avec un véritable LLM :** compréhension des modifications formulées librement, pertinence des clarifications et choix qualitatifs, parcours complet fermeture/réouverture/validation, gestion des erreurs du fournisseur et consommation réelle de tokens. Aucun de ces comportements n'est certifié par les simulations. Streamlit et plusieurs emails simultanément en attente restent hors périmètre.

## À faire
- [x] P0 — Conserver le brouillon après validation humaine (vérifié en simulation).
- [x] P0 — Valider le plan avant de générer les emails (vérifié en simulation).
- [x] P1 — Transmettre les observations météo (vérifié en simulation, intégration réelle encore à tester).
- [ ] P1 — Gérer plusieurs emails indépendants.
- [x] P1 — Afficher les statuts des validations dans le terminal (simulation).
- [x] Reprise et modification d'un événement sauvegardé (simulation).
- [x] Invalidation ciblée des décisions et accords devenus incompatibles après modification (simulation).
- [ ] Interface Streamlit.
- [ ] Affichage des conversations dans Streamlit avec les mêmes fonctions DAO.

## À vérifier
- [ ] Tester le parcours complet avec un véritable LLM.
- [ ] Vérifier la conformité finale au schéma AMMN.

## Terminé
- [x] Simplification de l'orchestration.
- [x] Partage des données avec event_data.
- [x] Correction des calculs financiers.
- [x] Optimisation des recherches et des filtres.
- [x] Présentation finale améliorée.

## Vérifications P0 du 25 novembre 2026

- **Simulation :** 138 tests ciblés réussis, puis **319 tests réussis** pour la suite complète, sans réseau ni appel LLM payant. Replay de l'incident `52c0c76e`, accord/refus, texte exact (espaces et ponctuation), absence de réécriture, validation du plan avant Writer et absence d'email si le plan est refusé. Quinze nouveaux cas de régression.
- Commandes : `.\.venv\Scripts\python.exe -m pytest tests/test_orchestration.py tests/test_presentation.py -q -p no:cacheprovider`, puis `.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider`.
- Permissions temporaires Windows : premiers essais bloqués ; exécutions finales autorisées hors bac à sable et réussies. `git diff --check` sans erreur.
- **Non testé avec un véritable LLM lors de cette correction P0 :** qualité des choix et du brouillon, extraction exhaustive des tâches/contraintes, comportement et consommation des fournisseurs après correction. Aucun essai réel lancé par Codex.
- Cinq agents IA, ReAct, `as_tool()`, finances et one-shot conservés ; catalogues et journal historique inchangés pendant cette correction. Aucun P1/P2 traité au-delà des statuts nécessaires aux P0.
- Détails et limites : [AUDIT_AMMN.md](AUDIT_AMMN.md). La simulation ne constitue pas une validation avec un véritable LLM.

## Corrections après le test réel étudiant — 25 novembre 2026

Trace utilisateur `eabe7b9b` et demande jointe examinées. Le journal contient le doublon `requirements.tasks`, sa correction au tour suivant et l'appel `get_meteo`. Les valeurs météo sont rapportées par l'utilisateur ; le journal LLM principal ne conserve pas l'observation API. Journaux et corrections P0 préexistants conservés.

- [x] 1. Délégations : `tasks` remonté depuis `requirements` seulement sans ambiguïté ; listes équivalentes acceptées, contradictions et autres contraintes invalides rejetées. Schéma et prompt clarifiés. **157 tests orchestration/filtres réussis** ; correction structurelle sans appel LLM de réparation.
- [x] 2. Présentation financière : HT sans TVA exclu explicitement du TTC, sous-total et trésorerie partiels, caution séparée et total non déterminable si des coûts manquent. Aucun faux total nul quand aucun TTC n'est calculable ; formules inchangées. **162 tests finance/présentation/orchestration réussis**.
- [x] 3. Météo : résultats validés de l'outil actuel capturés dans `event_data.weather` avec ville/date/source/statut ; déduplication par ville/date dans le run, transmission à la salle uniquement pour la ville/date pertinentes, bilan explicite. Erreur et hors horizon → `unavailable`, sans bloquer Planning. Pas d'appel systématique ajouté. **304 tests orchestration/outils/présentation réussis**.
- [x] 4. Localisation : `requirements.location_scope` distingue `city` de `province_fallback`. Les formulations strictes restent strictes ; les formulations souples reconnues cherchent d'abord la ville puis, seulement sans résultat, la même province du catalogue. `event_data.location_search` décrit ce repli administratif ; aucune distance, aucun voisinage garanti, aucun repli national. **188 tests orchestration/filtres/présentation réussis**, dont le parcours étudiant complet à Dinant et le parcours partiel sans salle dans la zone étendue, avec B-Sound Events à 3 000 EUR HT.

Commandes ciblées exécutées via `.\.venv\Scripts\python.exe -m pytest`, avec `-q -p no:cacheprovider` et accès temporaire pytest autorisé hors bac à sable. Réseau interdit par les fixtures ; aucun appel LLM réel ou payant.

**Validation finale :** `.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider` → **360 tests réussis** (41 cas ajoutés depuis les P0). `git diff --check` sans erreur. Les tests P0, financiers et filtres restent verts. Les catalogues et les journaux préexistants, dont le fichier de conflit, n'ont pas été modifiés.

**Mesures simulées :** 7 appels LLM / 3 délégations avec salle ; 8 appels / 2 délégations effectives sans salle, recherche autonome du prestataire comprise. Aucune réparation LLM pour le doublon `tasks`, aucun appel LLM pour la présentation financière ou l'élargissement local ; deux requêtes HTTP simulées par consultation météo ville/date (géocodage + prévision), sans duplication. Tokens réels après correction non mesurés.

**Limites :** le repli provincial est administratif, pas une garantie de proximité. Seules les formulations géographiques explicitement reconnues sont sécurisées en Python ; les autres dépendent de l'extraction du Supervisor. La météo d'une commune ne vaut pas pour une autre. La salle de Dinant impose ses prestataires `010`/`012`, donc B-Sound Events n'y est pas arbitrairement retenu. Aucun nouveau sous-système ni écart architectural ajouté ; qualité des décisions et consommation à vérifier avec un véritable LLM après ces corrections. Aucun nouvel essai réel lancé par Codex.

## Persistance locale et réouverture — 25 novembre 2026

- [x] DAO indépendant `dao/conversation_dao.py` : `create_conversation`, `save_conversation`, `list_conversations`, `load_conversation`. UUID4, JSON UTF-8 version 1, dates UTC ISO 8601, titre local sans LLM, validation explicite des types et chemins, écriture temporaire puis remplacement atomique.
- [x] Contrôleur : demande sauvegardée avant l'exécution, puis réponse et état final avec statut `completed`, `partial`, `refused`, `error` ou `interrupted`. En cas d'échec de sauvegarde finale, le résultat reste affichable en mémoire et l'ancien JSON valide reste intact. Aucune capture d'historique privé des agents, de configuration ou de logs techniques.
- [x] `event_data` reste l'état exact d'un run neuf. `event_details` complète la conversation avec les noms/offres sélectionnés et les montants calculés pendant ce run, avant libération des catalogues. Réouverture sans recalcul, sans catalogue, sans validation humaine ni modèle.
- [x] Menu Rich : nouvelle conversation, historique trié, consultation en lecture seule, retour au menu, quitter. Historique accessible même sans clé API. Fichiers corrompus signalés séparément.
- [x] Conversations personnelles et temporaires ignorés par Git ; `.gitkeep` conservé. README actualisé avec format réel, fonctions, erreurs et réutilisation Streamlit.

**Vérification simulée :** `.\.venv\Scripts\python.exe -m pytest tests/test_conversations.py tests/test_main.py -q -p no:cacheprovider` → **52 tests réussis** ; suite complète avec les mêmes options → **402 tests réussis**. Les tests terminal existants ont été adaptés au menu ; 38 nouveaux cas de persistance et 4 cas terminal nets supplémentaires. Réouverture vérifiée dans un nouveau processus : aucun import LLM/agents, aucun changement des octets ni de la date de modification. Test réel du code Supervisor avec réponses LLM simulées : B-Sound Events, identifiant `002`, kit numérique 6 et coût 3 000 EUR HT conservés sans catalogue à la relecture.

Tests entièrement dans les dossiers temporaires pytest, avec accès Windows autorisé hors bac à sable ; aucun appel API/LLM réel, aucune vraie conversation utilisateur créée. Les tests couvrent aussi refus, erreurs, interruption, indépendance de deux conversations, JSON incompatible/corrompu, chemins malveillants, erreur d'écriture et intégrité de l'ancien fichier lors d'un échec atomique. Les catalogues, traces historiques et corrections précédentes sont conservés ; aucun commit ni reset.

**Limites :** pas de reprise, d'édition, de migration de schéma, de concurrence multi-utilisateur ni de sauvegarde à chaque outil. Un arrêt brutal peut ne laisser que la demande initiale en statut `running`, signalée non finalisée à la consultation. Aucun nouveau test avec un véritable LLM. Avant Streamlit, définir la reprise et les invalidations ciblées ; la consultation seule peut déjà réutiliser le DAO tel quel.

## Console Windows dédiée — 25 novembre 2026

- [x] `python main.py` ouvre une nouvelle console Windows avec l'interpréteur courant et l'affichage `rich.console.Console` existant. Le processus initial attend et restitue le code de sortie.
- [x] `--current-console` conserve le terminal actuel et évite toute réouverture récursive ; les autres plateformes utilisent leur terminal actuel.
- Vérification : **56 tests menu/persistance réussis**, dont quatre nouveaux cas du lanceur (création de processus simulée, retour du code de sortie, absence de récursion, erreur de lancement). Aucun appel LLM ; ouverture visuelle de la fenêtre non vérifiée pendant ces tests. README actualisé.
