# Audit de conformité AMMN — Event-Planner-Agent

Date : 25 novembre 2026. **Note de conformité : 68/100.** L'architecture multi-agents annoncée existe ; les principaux écarts concernent les validations humaines, la conservation du brouillon approuvé et la transmission de la météo. Une refonte n'est pas nécessaire.

## Références et méthode

Documents retrouvés dans le dossier voisin `../Projet`, effectivement analysés :

- [Schéma AMMN](../Projet/Schéma_AMMN_SamainFlorian_DucourtieuxYohann.png) : lecture visuelle complète.
- [Fiche projet](../Projet/Fiche_projet_SamainFlorian_DucourtieuxYohann.pdf) : texte extrait et lecture visuelle des deux pages. La page 1 décrit les agents, les sorties et la validation ; la page 2 liste les risques.

Inspection du code courant non commité, des prompts, des tests et des journaux. Les **304 tests réussis** proviennent de l'exécution précédente dans cette conversation ; aucune nouvelle suite complète n'a été lancée. Pendant cet audit : contrôle local des catalogues et une reproduction isolée du problème Writer avec les réponses enregistrées, un LLM simulé et une approbation simulée. Aucun appel LLM ou API externe. Aucun fichier Python, catalogue JSON ou journal modifié.

« Conforme » signifie conforme au contrat vérifiable dans le code et aux preuves disponibles, pas fonctionnement garanti de tous les modèles. « Non vérifié » désigne notamment les services externes non appelés pendant l'audit.

## Engagements effectivement lus

- Le schéma représente un Supervisor LLM, quatre spécialistes LLM, un Event Listener, un Case File contenant résultats et historique, et un Final Milestone récapitulatif.
- Il distingue **validation du plan par le Supervisor** et **validation avant utilisation/envoi des emails par Writer**.
- La fiche, page 1, précise un ordre supplémentaire : **rapport final → validation humaine → génération des emails**. Approuver un brouillon déjà généré ne remplit pas cet engagement.
- La fiche confie Open-Meteo au Planificateur et les catalogues au Décisionnaire. Le schéma dessine en plus un lien entre Decision-Making et Open-Meteo. Cette différence documentaire doit être distinguée d'une panne du connecteur.
- Les deux documents confient les emails et le rapport au Rédacteur. La fiche attend des emails prêts à être envoyés et une fiche récapitulative ; elle n'impose pas un connecteur d'envoi automatique ni une mémoire persistante.

## Matrice de conformité

| Composant | Prévu | Implémenté | Conforme | Écart | Correction nécessaire |
|---|---|---|---|---|---|
| Event Listener | Recevoir la demande | Saisie terminal dans `main.py`, un run par demande | Conforme | Pas de service d'écoute permanent, non requis | Aucune |
| Supervisor | Déléguer, coordonner, respecter les contraintes, restituer | Agent LLM avec quatre outils de délégation ; état et contrôles Python ; bilan vérifié | Partiellement conforme | Pas de validation humaine du plan ; exhaustivité des tâches encore dépendante de l'extraction | P0 : validation du plan/rapport avant emails |
| Budgetary | Répartir/ajuster le budget | LLM produisant des proportions par catégorie ; contrôles Python | Partiellement conforme | Allocation initiale réelle ; un succès n'est plus révisable dans le même run, même après retour humain | Prévoir une invalidation ciblée seulement si le périmètre exige une révision après refus |
| Planning | Date, horaires et météo pertinente | LLM ; `get_next_day`, `get_meteo` ; sortie start/end | Partiellement conforme | Aucun résultat météo partagé ; prompt sans règle explicite d'usage météo | P1 : usage pertinent et transmission d'un résumé météo |
| Decision-Making | Choisir les meilleures options depuis les données | LLM choisissant des identifiants parmi des candidats filtrés ; outil limité à la catégorie déléguée | Partiellement conforme | Aucun accès météo direct ni indirect fiable ; choix qualitatif parmi un sous-ensemble, pas optimum global démontré | P1 : fournir la météo utile ; conserver le choix qualitatif LLM |
| Writer | Emails et rapport à partir des résultats | LLM, contexte consolidé, `ask_user`, documents demandés seulement | Partiellement conforme | Régénération après approbation ; rapport standard rendu par Python ; un seul résultat documents | P0 : conserver le texte approuvé ; P1 : documents multiples si demandés |
| Connecteur Open-Meteo | Consulter les prévisions réelles | Géocodage et prévisions via `requests`, délais et erreurs gérés | Conforme | Le code refuse les dates hors de sa fenêtre de 16 jours ; limitation déclarée | Ne pas inventer de météo hors horizon |
| Disponibilité actuelle Open-Meteo | Service exploitable en démonstration | Tests existants simulant succès et échecs | Non vérifié | Aucun appel réseau pendant l'audit | Un essai ciblé ultérieur si nécessaire, sans LLM |
| Catalogues JSON | Salles, traiteurs, prestataires fictifs | 150 salles, 100 traiteurs, 40 prestataires ; identifiants uniques et références fournisseurs présentes | Conforme | Données de démonstration, pas disponibilités ni coordonnées réelles | Aucune refonte ; signaler les données manquantes |
| Recherche et contraintes | Ville, capacité, type, budget, horaires, contraintes obligatoires | Normalisation, contrôles métier et financiers avant limitation ; erreurs explicites côté agents | Partiellement conforme | Une contrainte naturelle non extraite peut manquer ; les fonctions publiques gardent un repli catalogue intégral | Garder les agents sur les outils stricts ; tests ciblés pour toute formulation réellement défaillante |
| Case File | Résultats et contexte du cas | `EventRun.event_data`, catalogues chargés par run, historiques locaux des agents | Partiellement conforme | Pas d'historique commun complet ; météo et accord global absents ; brouillon accepté perdu en cas d'échec final | Ajouter seulement les informations nécessaires dans l'état du run |
| Validation du plan/rapport | Accord humain avant génération des emails | Aucun champ d'accord global, aucune barrière avant Writer | Non conforme | La seule validation humaine concerne le brouillon | P0 : accord global explicite avec traitement du refus |
| Validation des emails | Accord avant utilisation/envoi | `ask_user`, contrôle du texte exact, aucun outil d'envoi | Partiellement conforme | Une approbation peut être suivie d'une erreur et d'une perte du brouillon | P0 : terminer sur le texte approuvé, sans nouvel appel LLM |
| Final Milestone | Fiche cohérente avec toutes les décisions et les emails | Résumé lisible, montants Python, contraintes, réserves et résultats partiels | Partiellement conforme | Accord global absent ; approbation positive peu visible ; documents multiples et météo non couverts | P1 : compléter les statuts et documents après correction des validations |
| Bornes d'exécution | Éviter les boucles | Limites d'appels/délégations, réparation bornée, succès conservés | Conforme | La borne d'appels n'est pas une garantie de latence courte | P2 : réduire les appels et schémas inutiles |

## Les cinq composants sont bien des agents IA

Tous utilisent `Agent.run()` → `model.llm.chat()` → client de complétions compatible OpenAI, avec prompts et outils propres. `as_tool()` expose une délégation à un agent, pas un calcul Python remplaçant son raisonnement. Les journaux existants contiennent des appels pour **les cinq noms d'agents**. Partager la classe Agent et le fournisseur ne retire pas leur spécialisation. Une tâche sautée parce que son résultat est déjà imposé ou son budget absent n'appelle légitimement pas le modèle.

| Agent | Prompt et rôle réel | Entrées | Outils réellement exposés | Sortie et échanges |
|---|---|---|---|---|
| Supervisor | `PROMPT_SUPERVISOR`, dans `model/event_orchestrator.py` : choisit tâches et ordre | Demande et observations de ses délégations | budgetary_agent, planning_agent, decision_making_agent, writer_agent | Décisions de délégation ; réponse finale remplacée par le bilan Python vérifié |
| Budgetary | `PROMPT_BUDGETARY` : répartition qualitative par pas de 5 % | Plafond, effectif, préférences, catégories demandées, demande originale | Aucun | JSON venue/catering/provider, validé et enregistré dans budget ; montants calculés séparément en Python |
| Planning | `PROMPT_PLANNING` : dates futures, horaires, jours imposés | Demande, exigences, date courante et contraintes de salle déjà choisie | get_next_day, get_meteo | JSON start/end ; météo consultée éventuellement dans son historique local, pas dans sa sortie métier |
| Decision-Making | `PROMPT_DECISION_MAKING` : adéquation qualitative après contraintes obligatoires | Demande, filtres, candidats, planning, finances, salle selon catégorie | get_venues OU get_caterers OU get_providers dans une délégation | id, éventuellement formula_id/kit_id ; appartenance et compatibilité vérifiées avant enregistrement |
| Writer | `PROMPT_WRITER` : rédiger sans inventer et demander l'accord | Demande, décisions développées avec noms/offres, coûts, exigences et tâches non résolues | ask_user uniquement | Texte ; document stocké avec approved si succès ou refus, mais pas après l'erreur post-approbation |

Repères : `model/agents/agents.py:12`, `model/agents/prompt.py`, `model/event_orchestrator.py:80`, `model/agent.py:19`, `model/llm.py:96`.

## Données, contexte et Final Milestone

Le Case File limité à une exécution suffit pour une demande scolaire traitée en une fois. `remember()` fusionne les contraintes, les décisions validées sont conservées et les détails sont reconstruits depuis les catalogues du même run. Les catégories indépendantes peuvent continuer après un échec. Les identifiants de fournisseurs imposés contrôlés pendant cet audit correspondent tous à des entrées existantes. Rien ne justifie une base de mémoire persistante.

Les manques sont **dans le run** : absence d'accord global, météo non partagée, perte du brouillon approuvé en cas d'erreur, et cardinalité limitée. `record()` remplace documents par une liste d'un élément et les délégations documents réussies sont réutilisées ; demander des emails distincts à plusieurs entreprises n'est pas réellement modélisé. Même limite pour plusieurs prestataires techniques : le champ providers est une liste, mais la sélection conserve un seul élément. Un long texte contenant plusieurs emails ne fournit pas des validations distinctes par destinataire.

`consolidated()` et `summary()` reprennent noms, date/horaires, offres, coûts connus, caution séparée, réserves, exclusivités de salle, fin de musique et échecs. `model/finance.py` distingue HT/TTC, TVA connue/inconnue, nettoyage, déplacements et caution ; le bilan ne laisse pas le LLM refaire les calculs. Les tests financiers précédemment réussis sont une preuve simulée, pas un devis commercial. Coordonnées des destinataires et disponibilités ne sont pas dans les catalogues.

Le statut success désigne une validation technique, **pas l'accord humain du plan**. Un document accepté est présenté comme « Document », un refus comme « Brouillon refusé » ; le statut positif devrait être explicite. Le rapport standard déterministe protège la cohérence des chiffres et évite une synthèse LLM inutile : différence d'implémentation justifiée. Toutefois, le rôle de rédaction systématique du rapport annoncé pour Writer n'est pas exécuté par défaut ; à expliciter dans la présentation ou à activer pour un rapport rédigé expressément demandé, sans modifier les montants validés.

## Incident Writer : cause confirmée et correction minimale proposée

Trace réelle : demande `52c0c76e`, 25 novembre 2026. À 12:26:52, Writer appelle ask_user avec un texte comprenant une introduction, le brouillon et une question finale de validation. Le journal conserve l'appel, **pas la réponse humaine** ; l'approbation est rapportée par l'utilisateur. À 12:27:28, un autre modèle renvoie une version différente : introduction et question supprimées, « a retenue » corrigé en « a retenu », espaces de montants modifiées.

Chemin causal dans le code :

1. `event_state.py:478` garde `approval.content` et le booléen renvoyé par ask_user.
2. Le callback completed arrête la boucle d'outils dès que l'utilisateur a répondu.
3. `agent.py:131` marque néanmoins l'agent partial, puis lance un **nouvel appel LLM de restitution** pour ce spécialiste.
4. `event_state.py:489` exige l'égalité après strip entre cette restitution et le texte approuvé. Les changements observés violent cette règle. Il s'agit d'une **erreur d'égalité du texte, pas d'une erreur de syntaxe JSON**, malgré le code générique invalid_output.
5. Le résultat devient error ; `record()` n'enregistre alors aucun document. L'accord et le texte restent dans une variable locale puis disparaissent de l'état utile.

Reproduction isolée pendant cet audit : les deux réponses réelles ont été rejouées avec approbation simulée true. Résultat exact : `status=error`, `code=invalid_output`, message « Le texte final doit être exactement le brouillon approuvé. », **documents=[]**, deux appels LLM simulés et un ask_user simulé. Le test existant `test_email_requires_approval_of_exact_returned_text` considère déjà cette erreur comme attendue : les 304 tests verts ne validaient donc pas la conservation d'un brouillon accepté malgré une tentative de réécriture.

**Correction proposée, non implémentée :** dès le retour de ask_user, rendre terminal le résultat documentaire en utilisant directement `approval.content`. Si approved=true, conserver ce texte exact et le statut accepté ; sinon conserver le même texte avec le statut refusé. Court-circuiter la restitution LLM dans cette branche seulement. Ne pas assouplir la comparaison pour accepter une réécriture ; ne pas redemander l'accord pour le même texte. Le prompt doit fournir uniquement le brouillon dans l'argument message, puisque l'outil pose lui-même la question oui/non. Ne jamais supprimer rétroactivement des passages d'un texte déjà approuvé.

Critères de validation du futur correctif : aucun appel LLM après ask_user ; conservation exacte, y compris ponctuation et espaces ; accord/refus stockés ; aucun envoi ; choix de salle conservé ; absence d'approbation = pas de document présenté comme accepté. Adapter le test existant et ajouter le replay réel, sans réseau.

## Priorités et complexité

| Priorité | Correction minimale | Complexité estimée | Validation attendue |
|---|---|---|---|
| **P0** | Conserver le brouillon accepté/refusé et terminer sans régénération | Faible : branche de terminaison dans Agent et traitement documentaire dans EventRun | Replay Writer ci-dessus, un seul appel de rédaction, zéro appel post-accord |
| **P0** | Faire valider le plan/rapport consolidé avant toute génération d'email ; stocker cet accord dans event_data | Faible à moyenne : une barrière Python utilisant ask_user, sans nouvel agent | Accord → Writer autorisé ; refus/annulation → aucun email généré ; plan conservé et statut explicite |
| **P1** | Conserver une observation météo compacte et la transmettre au choix de salle quand pertinente | Moyenne : capture du résultat d'outil, contexte et prompt ciblés | Date dans l'horizon, intempéries, panne API et hors horizon ; aucun effet inventé |
| **P1** | Permettre les documents distincts réellement demandés, avec approbation par document et destinataire connu ou signalé manquant | Moyenne ; éviter une refonte du registre tant que le besoin reste borné | Deux emails différents conservés, refus de l'un sans perte de l'autre |
| **P1** | Rendre les statuts du plan et des documents visibles dans la fiche finale ; clarifier le rôle du rapport Writer | Faible | Le bilan distingue proposé, validé techniquement, approuvé/refusé humainement et non résolu |
| **P1 conditionnel** | Réviser seulement les décisions affectées si la démonstration exige une modification après refus | Moyenne | Pas de réutilisation d'un ancien accord pour un plan modifié ; décisions indépendantes conservées |
| **P2** | Alléger les schémas répétés, éviter une délégation déjà réussie et choisir une route modèle plus stable si disponible | Faible à moyenne | Mesurer les appels/tokens ; ne pas augmenter simplement les limites |

Le refus d'un email est déjà conservé et arrête son traitement, sans envoi ni seconde approbation. Un appel LLM de restitution reste inutile même dans ce cas. Le refus du **plan global n'a aucun comportement implémenté**, puisque cette question n'est jamais posée. Pour la correction minimale, arrêter proprement les emails et rendre un bilan refusé suffit ; une boucle de révision n'est pas imposée par les documents.

Pour la météo, une observation partagée suffit fonctionnellement ; si une fidélité littérale au lien Decision-Making → Open-Meteo du schéma est exigée, exposer aussi l'outil au Decision-Making de salle. La fiche n'exige ce connecteur que pour Planning : ne pas confondre cette différence avec une absence totale d'Open-Meteo.

## Note et prochaine action

Barème d'audit explicite, non note académique officielle :

| Critère | Points acquis / maximum | Justification |
|---|---|---|
| Agents IA, prompts et orchestration | 23/25 | Cinq agents réels ; rôles principaux respectés |
| Outils, catalogues et météo utile | 14/20 | JSON fonctionnels ; connecteur météo présent, exploitation transversale incomplète |
| État du cas et contraintes | 11/15 | Décisions conservées, mais informations/accords manquants et cardinalité limitée |
| Validations humaines et séquencement | 5/20 | ask_user présent ; accord global absent et approbation documentaire fragile |
| Fiche finale, finances et documents | 10/15 | Montants et bilan protégés ; documents et statuts incomplets |
| Bornes et preuves de tests | 5/5 | Limites explicites, tests simulés et incident reproduit |
| **Total** | **68/100** | Architecture respectée, parcours humain encore incomplet |

**Prochaine action recommandée :** corriger d'abord la terminaison Writer avec le replay enregistré, puis ajouter la validation globale avant Writer, puis traiter le transfert météo. Ces corrections ne sont pas réalisées pendant cet audit. Les deux P0 bloquent une démonstration fidèle du parcours complet annoncé ; la météo et les emails multiples limitent les scénarios que l'on peut honnêtement présenter comme couverts.
