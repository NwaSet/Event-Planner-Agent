# To Do — Event-Planner-Agent

## À faire
- [x] P0 — Conserver le brouillon après validation humaine (vérifié en simulation).
- [x] P0 — Valider le plan avant de générer les emails (vérifié en simulation).
- [x] P1 — Transmettre les observations météo (vérifié en simulation, intégration réelle encore à tester).
- [ ] P1 — Gérer plusieurs emails indépendants.
- [ ] P1 — Afficher les statuts des validations.
- [ ] Reprise et modification d'un événement sauvegardé.
- [ ] Invalidation ciblée des décisions et accords devenus incompatibles après modification.
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
