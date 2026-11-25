"""Calculs sur les prix du catalogue. Aucun taux fiscal ni trajet n'est inventé."""

from decimal import Decimal, ROUND_HALF_UP


FINANCIAL_FIELDS = ("catering_distance_km", "provider_distance_km", "provider_vehicles", "provider_vat_rate")


def money(value):
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def amount(value):
    return float(money(value))


def venue_cost(row, period=None):
    prices = row["prix"]
    selected = period or "journee"
    rental = money(prices[f"location_{selected}_ttc"])
    cleaning = money(prices["nettoyage_obligatoire_ttc"])
    deposit = money(prices["caution_remboursable"])
    return {"period": selected, "period_assumed": period is None,
            "rental_ttc": amount(rental), "cleaning_ttc": amount(cleaning),
            "known_ttc": amount(rental + cleaning), "deposit": amount(deposit),
            "cash_required": amount(rental + cleaning + deposit),
            "evening_ttc": amount(money(prices["location_soiree_ttc"]) + cleaning)}


def catering_cost(row, offer, guests=None, distance=None):
    fees = row["frais_supplementaires"]
    meal = money(Decimal(str(offer["prix_par_personne_ttc"])) * guests) if guests is not None else None
    delivery = money(fees["livraison_forfait_ttc"])
    travel = money(Decimal(str(fees["deplacement_eur_ttc_km"])) * Decimal(str(distance))) if distance is not None else None
    return {"unit_ttc": amount(offer["prix_par_personne_ttc"]), "guests": guests,
            "meal_ttc": amount(meal) if meal is not None else None,
            "delivery_ttc": amount(delivery), "travel_ttc": amount(travel) if travel is not None else None,
            "known_ttc": amount(meal + delivery + (travel or 0)) if meal is not None else None}


def provider_cost(row, offer, inputs):
    kit = money(offer["prix_estime_eur_ht"])
    distance, vehicles, rate = (inputs.get(field) for field in FINANCIAL_FIELDS[1:])
    travel = None
    if distance == 0:
        travel = money(0)
    elif distance is not None and vehicles is not None:
        travel = money(Decimal(str(row["deplacement"]["prix_simule_eur_ht_km_par_vehicule"]))
                       * Decimal(str(distance)) * vehicles)
    known_ht = kit + (travel or 0)
    vat = money(known_ht * Decimal(str(rate))) if rate is not None else None
    return {"kit_ht": amount(kit), "travel_ht": amount(travel) if travel is not None else None,
            "known_ht": amount(known_ht), "vat_rate": rate, "vat": amount(vat) if vat is not None else None,
            "known_ttc": amount(known_ht + vat) if vat is not None else None}


def fits_budget(category, row, offer, filters, inputs):
    """Comparer les frais calculables au plafond, sans certifier les frais inconnus."""
    if "max_budget" not in filters:
        return True
    if category == "venue":
        price = venue_cost(row, filters.get("rental_period"))["known_ttc"]
    elif category == "catering":
        price = catering_cost(row, offer, filters.get("guests"), inputs.get("catering_distance_km"))["known_ttc"]
    else:
        costs = provider_cost(row, offer, inputs)
        price = costs["known_ttc"] if costs["known_ttc"] is not None else costs["known_ht"]
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
            lines.append(f"Salle ({period}) : location {euros(detail['rental_ttc'])} + nettoyage obligatoire "
                         f"{euros(detail['cleaning_ttc'])} = {euros(detail['known_ttc'])} TTC.")
            lines.append(f"Caution remboursable : {euros(detail['deposit'])}, hors dépense. "
                         f"À mobiliser pour la salle, caution comprise : {euros(detail['cash_required'])}.")
            if detail["period_assumed"]:
                lines.append(f"Période à confirmer : le tarif soirée avec nettoyage serait de {euros(detail['evening_ttc'])} TTC.")
        elif category == "catering":
            lines.append(f"Traiteur : {euros(detail['unit_ttc'])} TTC par personne ; livraison forfaitaire "
                         f"{euros(detail['delivery_ttc'])} TTC.")
            if detail["meal_ttc"] is not None:
                lines.append(f"Repas pour {detail['guests']} personnes : {euros(detail['meal_ttc'])} TTC.")
            if detail["travel_ttc"] is not None:
                lines.append(f"Déplacement du traiteur : {euros(detail['travel_ttc'])} TTC, en plus de la livraison.")
            if detail["known_ttc"] is not None:
                lines.append(f"Sous-total traiteur connu : {euros(detail['known_ttc'])} TTC.")
        else:
            lines.append(f"Kit prestataire : {euros(detail['kit_ht'])} HT.")
            if detail["travel_ht"] is not None:
                lines.append(f"Déplacement prestataire : {euros(detail['travel_ht'])} HT.")
            lines.append(f"Sous-total prestataire connu : {euros(detail['known_ht'])} HT.")
            if detail["vat"] is not None:
                lines.append(f"TVA au taux fourni de {detail['vat_rate'] * 100:g} % : {euros(detail['vat'])} ; "
                             f"prestataire : {euros(detail['known_ttc'])} TTC sur les postes connus.")
    if costs["details"]:
        lines.append(f"Sous-total des postes TTC connus : {euros(costs['known_ttc_subtotal'])}, hors caution et frais non chiffrés.")
        lines.append(f"Somme connue à mobiliser, caution comprise : {euros(costs['known_cash_required'])}.")
        lines.extend(costs["missing"])
        if costs["known_over_budget"]:
            lines.append("Attention : les seuls postes TTC connus dépassent déjà le budget plafond.")
        lines.append(costs["warning"])
    return lines
