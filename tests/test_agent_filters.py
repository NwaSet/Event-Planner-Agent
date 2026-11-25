import pytest

from model.event_state import EventRun
from model.agents import tools
from model.agents.search import search_candidates


def test_agent_filters_normalize_unambiguous_types_before_validation():
    result = search_candidates("venue", {"ids": "salle_001", "guests": "40", "max_budget": "3000,50",
                                         "rental_period": "Soirée", "accessible": "false", "limit": "1"})
    assert result["status"] == "matched" and result["candidates"][0]["id"] == "salle_001"
    assert result["filters"] == {"ids": ["salle_001"], "guests": 40, "max_budget": 3000.5,
                                 "rental_period": "soiree", "accessible": False, "limit": 1}
    # Le contrat public reste strict et conserve son repli intégral.
    assert len(tools.get_venues(ids=["salle_001"], guests="40")) == 150


def test_provider_identifier_keeps_its_leading_zeros():
    result = search_candidates("provider", {"ids": "005"})
    assert result["status"] == "matched" and result["candidates"][0]["id"] == "005"
    assert search_candidates("provider", {"ids": [5]})["status"] == "invalid_filters"


@pytest.mark.parametrize("filters", [{"guests": True}, {"guests": "100 à 150"}, {"guests": 40.5},
                                     {"max_budget": "NaN"}, {"accessible": "parfois"}])
def test_ambiguous_or_invalid_types_do_not_relax_constraints(filters):
    result = search_candidates("venue", filters)
    assert result["status"] == "invalid_filters" and result["candidates"] == []


def test_normalized_requirements_and_filters_do_not_create_false_conflicts():
    run = EventRun("Salle")
    run.remember({"guests": 40, "venue_filters": {"accessible": False, "rental_period": "soiree"}})
    run.remember({"guests": "40", "venue_filters": {"accessible": "false", "rental_period": "Soirée"}})
    assert run.context("venue")["filters"]["guests"] == 40
    assert run.context("venue")["filters"]["accessible"] is False
    with pytest.raises(ValueError, match="Contrainte"):
        run.remember({"guests": "20"})


@pytest.mark.parametrize("user_request", ["Soirée musicale à Namur", "Soirée électronique avec DJ à Namur",
                                         "Soirée musicale, pas un concert, à Namur"])
def test_musical_party_cannot_be_filtered_as_an_unrequested_concert(user_request):
    run = EventRun(user_request)
    with pytest.raises(ValueError, match="concert"):
        run.remember({"event_type": "concert"})
    assert "event_type" not in run.event_data["requirements"]
    run.remember({"city": "Namur", "music_required": True, "rental_period": "soiree"})
    context = run.context("venue")
    assert search_candidates("venue", context["filters"], context=context)["status"] == "matched"
    assert search_candidates("venue", {"city": "Namur", "event_type": "concert"}, context=context)["status"] == "invalid_filters"


def test_explicit_concert_remains_a_mandatory_catalogue_filter():
    run = EventRun("Une soirée musicale avec un concert à Bruxelles")
    run.remember({"event_type": "concert", "city": "Bruxelles"})
    result = search_candidates("venue", run.context("venue")["filters"], context=run.context("venue"))
    assert result["status"] in ("matched", "no_match")
    assert all("concert" not in row["types_evenements_interdits"] for row in result["candidates"])
