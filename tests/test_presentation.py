from model.event_state import EventRun


def test_complete_summary_displays_names_local_dates_and_constraints_without_json():
    run = EventRun("Soirée")
    run.remember({"rental_period": "soiree"})
    for task, result in (
        ("planning", {"start": "2026-12-19T20:00", "end": "2026-12-20T01:00"}),
        ("venue", {"id": "salle_001"}),
        ("catering", {"id": "traiteur_001", "formula_id": "formule_01"}),
        ("provider", {"id": "002", "kit_id": 1}),
    ):
        run.record(task, {"status": "success", "result": result})
    output = run.summary("")
    assert "19/12/2026 à 20:00" in output and "20/12/2026 à 01:00 (heure locale)" in output
    assert all(run.selected_row(category)["nom"] in output for category in ("venue", "catering", "provider"))
    assert "Fin de la musique autorisée par la salle : 02:00" in output
    assert "Traiteurs autorisés par la salle" not in output and "Prestataires techniques autorisés par la salle" not in output
    assert next(row["nom"] for row in run.catalogs["catering"] if row["id"] == "traiteur_034") not in output
    assert next(row["nom"] for row in run.catalogs["provider"] if row["id"] == "003") not in output
    assert "2 425,00 EUR TTC" in output and "tout compris" in output
    assert "Coût TTC total : non déterminable" in output and "aucune réservation confirmée" in output
    assert all(internal not in output for internal in ("{", "}", "salle_001", "traiteur_001", "heure_fin_musique"))


def test_venue_summary_never_invents_supplier_restrictions():
    run = EventRun("Salle")
    run.record("venue", {"status": "success", "result": {"id": "salle_001"}})
    assert "contraintes" not in run.selected_row("venue")
    assert "autorisé" not in run.summary("").replace("autorisée", "")


def test_partial_summary_preserves_document_and_explains_pending_tasks():
    run = EventRun("Synthèse partielle")
    run.remember(tasks=["planning", "venue", "documents"])
    run.record("planning", {"status": "error", "code": "llm_error", "error": "Fournisseur indisponible"})
    run.record("documents", {"status": "success", "result": {"content": "Brouillon conservé.", "approved": None}})
    output = run.summary("Limite atteinte.")
    assert "Résultat partiel" in output and "Non résolu — planning : Fournisseur indisponible" in output
    assert "Non résolu — salle : tâche non terminée" in output and "Document :\nBrouillon conservé." in output
    assert "{" not in output
