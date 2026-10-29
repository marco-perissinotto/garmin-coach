"""Days to each event and training phase by weeks out (PLAN §7)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..athlete import Event


@dataclass
class EventView:
    event: Event
    days_to: int
    weeks_to: float
    phase: str
    focus: str


def phase(days_to: int) -> tuple[str, str]:
    """(phase, suggested focus) for a target event ``days_to`` days away. ≥12 weeks base, 12–4 build, 4–2 peak,
    <2 taper. Text in Italian: it goes into the packs as is."""
    w = days_to / 7
    if days_to < 0:
        return "passato", ""
    if days_to == 0:
        return "giorno gara", "Gara: riscaldamento, ritmo pianificato, alimentazione"
    if days_to <= 7:
        return "settimana gara", "Freschezza: volume ridotto, pochi richiami brevi a ritmo gara"
    if w < 2:
        return "scarico", "Ridurre il volume del 30–50%, mantenere l'intensità con richiami brevi"
    if w < 4:
        return "picco", "Sedute specifiche a ritmo gara, recupero completo tra le sedute chiave"
    if w < 12:
        return "costruzione", "Aumentare gradualmente volume e lavori specifici, una settimana di scarico ogni 3–4"
    return "base", "Volume aerobico, tecnica, forza; intensità limitata"


def event_views(events: list[Event], today: date) -> list[EventView]:
    out = []
    for e in events:
        days = (e.date - today).days
        if days < 0:
            continue
        ph, focus = phase(days)
        out.append(EventView(e, days, round(days / 7, 1), ph, focus))
    return out
