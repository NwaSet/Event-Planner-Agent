"""Calculs sur les prix du catalogue. Aucun taux fiscal ni trajet n'est inventé."""

from decimal import Decimal, ROUND_HALF_UP



def money(value):
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def amount(value):
    return float(money(value))


def venue_cost(row, period=None):
    # Garder la forme des montants pour les lecteurs d'anciens états : frais et caution à zéro.
    prices = row["prix"]
    selected = period or "journee"
    rental = money(prices[f"location_{selected}_ttc"])
    return {"period": selected, "period_assumed": period is None,
            "rental_ttc": amount(rental), "cleaning_ttc": 0,
            "known_ttc": amount(rental), "deposit": 0,
            "cash_required": amount(rental),
            "evening_ttc": amount(prices["location_soiree_ttc"])}


def catering_cost(row, offer, guests=None, distance=None):
    meal = money(Decimal(str(offer["prix_par_personne_ttc"])) * guests) if guests is not None else None
    return {"unit_ttc": amount(offer["prix_par_personne_ttc"]), "guests": guests,
            "meal_ttc": amount(meal) if meal is not None else None,
            "delivery_ttc": 0, "travel_ttc": 0,
            "known_ttc": amount(meal) if meal is not None else None}


def provider_cost(row, offer, inputs=None):
    """Prix fictif TTC tout compris ; les anciens paramètres fiscaux n'ajoutent rien."""
    kit = amount(offer["prix_ttc"])
    return {"kit_ttc": kit, "known_ttc": kit}


def fits_budget(category, row, offer, filters):
    """Comparer les frais calculables au plafond, sans certifier les frais inconnus."""
    if "max_budget" not in filters:
        return True
    if category == "venue":
        price = venue_cost(row, filters.get("rental_period"))["known_ttc"]
    elif category == "catering":
        price = catering_cost(row, offer, filters.get("guests"))["known_ttc"]
    else:
        price = provider_cost(row, offer)["known_ttc"]
    return price is not None and money(price) <= money(filters["max_budget"])


def euros(value):
    return f"{amount(value):,.2f}".replace(",", " ").replace(".", ",") + " EUR"


def financial_lines(costs):
    lines = []
    if costs["budget_total"] is not None:
        lines.append("Budget plafond : " + euros(costs["budget_total"]) + ".")
    if costs["allocations"]:
        labels = {"venue": "salle", "catering": "traiteur", "provider": "prestataire"}
        lines.append("Répartition : " + "; ".join(f"{labels[key]} {euros(value)}" for key, value in costs["allocations"].items()) + ".")
    for category, detail in costs["details"].items():
        if category == "venue":
            period = "soirée" if detail["period"] == "soiree" else "journée"
            lines.append(f"Salle ({period}) : {euros(detail['known_ttc'])} TTC, tout compris.")
            if detail["period_assumed"]:
                lines.append(f"Période à confirmer : le tarif soirée serait de {euros(detail['evening_ttc'])} TTC.")
        elif category == "catering":
            lines.append(f"Traiteur : {euros(detail['unit_ttc'])} TTC par personne.")
            if detail["meal_ttc"] is not None:
                lines.append(f"Repas pour {detail['guests']} personnes : {euros(detail['meal_ttc'])} TTC.")
        else:
            lines.append(f"Kit prestataire : {euros(detail['kit_ttc'])} TTC, tout compris.")
    if costs["details"]:
        has_ttc = any(key.endswith("_ttc") for key in costs["known"])
        incomplete = bool(costs["missing"])
        if has_ttc:
            qualifier = " (partiel)" if incomplete else ""
            lines.append(f"Sous-total des postes TTC connus{qualifier} : {euros(costs['known_ttc_subtotal'])}.")
            lines.append(f"Somme{' partielle' if incomplete else ' connue'} à mobiliser sur ces seuls postes : "
                         f"{euros(costs['known_cash_required'])}.")
        else:
            lines.append("Aucun sous-total TTC calculable sur les postes sélectionnés.")
        if incomplete or not has_ttc:
            lines.append("Coût TTC total : non déterminable avec les informations disponibles.")
        lines.extend(costs["missing"])
        if costs["known_over_budget"]:
            lines.append("Attention : les seuls postes TTC connus dépassent déjà le budget plafond.")
        lines.append(costs["warning"])
    return lines
