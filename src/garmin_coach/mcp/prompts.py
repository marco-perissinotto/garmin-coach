"""The five MCP prompts (PLAN §9): each embeds the packs it needs, so the model does not have to guess which tools
to call. Instructions follow the owner's ``advice_language`` (Italian or English; other languages get the English
text plus "answer in <language>"). The packs themselves are Italian (D-044)."""

from __future__ import annotations

import re
from pathlib import Path

from ..athlete import load_inputs
from .tools import CoachService

METRICS_DOC = Path(__file__).resolve().parents[3] / "docs" / "METRICS.md"

# metric name or alias (lower case) -> heading of the docs/METRICS.md section that explains it
METRIC_SECTIONS = {
    "load": "Daily load", "carico": "Daily load", "trimp": "Daily load", "training load": "Daily load",
    "atl": "ATL, CTL, TSB", "ctl": "ATL, CTL, TSB", "tsb": "ATL, CTL, TSB", "form": "ATL, CTL, TSB",
    "forma": "ATL, CTL, TSB", "fitness": "ATL, CTL, TSB", "fatigue": "ATL, CTL, TSB",
    "acwr": "ACWR", "monotony": "Monotony and strain (Foster)", "monotonia": "Monotony and strain (Foster)",
    "strain": "Monotony and strain (Foster)", "hrv": "Recovery", "rhr": "Recovery", "resting hr": "Recovery",
    "sleep": "Recovery", "sonno": "Recovery", "body battery": "Recovery", "recovery": "Recovery",
    "recupero": "Recovery", "readiness": "Readiness (heuristic)", "prontezza": "Readiness (heuristic)",
    "vo2max": "Fitness indicators (instead of VO2max)", "efficiency": "Fitness indicators (instead of VO2max)",
    "efficienza": "Fitness indicators (instead of VO2max)", "power": "Fitness indicators (instead of VO2max)",
    "potenza": "Fitness indicators (instead of VO2max)", "zones": "Fitness indicators (instead of VO2max)",
    "zone": "Fitness indicators (instead of VO2max)", "lthr": "Fitness indicators (instead of VO2max)",
    "ftp": "Fitness indicators (instead of VO2max)", "phase": "Event phases", "fase": "Event phases",
    "taper": "Event phases", "events": "Event phases",
}

_TEXT = {
    "it": {
        "daily_checkin": (
            "Fai il check-in di oggi come mio allenatore, usando il brief del giorno, il mio profilo (storia e regole "
            "concordate) e gli eventi qui sotto. Dimmi: 1) l'allenamento di oggi (tipo, durata, intensità in zone "
            "FC, potenza o passo), 2) perché, in poche righe, 3) cosa osservare e quando cambiare piano. Se i dati "
            "non sono aggiornati o lo storico è ancora in caricamento, dillo prima di tutto."),
        "weekly_review": (
            "Fai la revisione della settimana usando i dati degli ultimi 7 e 28 giorni qui sotto: cosa è andato "
            "bene, cosa no (carico, intensità, recupero, sonno), e cosa cambiare la prossima settimana tenendo "
            "conto degli eventi in programma e delle regole concordate."),
        "plan_week": (
            "Prepara il piano dei prossimi 7 giorni, giorno per giorno (seduta, durata, intensità), rispettando la "
            "mia disponibilità, i vincoli, le regole concordate e la fase verso il prossimo evento. Parti dal carico "
            "e dal recupero attuali qui sotto e spiega in breve la logica della settimana."),
        "race_readiness": (
            "Valuta come arrivo all'evento «{event}»: fase attuale, cosa fare da qui alla gara (incluso lo scarico), "
            "forma prevista rispetto all'obiettivo e ai risultati passati nella mia storia. Sii esplicito sulle "
            "incertezze: le metriche sono stime locali, non quelle proprietarie di Garmin."),
        "race_none": "Non ci sono eventi futuri in events.yaml: dimmelo e chiedimi quale gara preparare.",
        "explain_metric": (
            "Spiegami la metrica «{name}» in modo semplice: cosa misura, come è calcolata qui (vedi la definizione "
            "sotto), come leggere il mio valore attuale nel brief e quali limiti ha. Niente formule se non servono."),
        "answer": "Rispondi in italiano, in modo diretto e concreto.",
    },
    "en": {
        "daily_checkin": (
            "Do today's check-in as my coach, using today's brief, my profile (history and agreed rules) and the "
            "events below. Tell me: 1) today's session (type, duration, intensity as HR/power/pace zones), 2) why, in "
            "a few lines, 3) what to watch and when to change the plan. If the data is stale or the history is still "
            "loading, say so first."),
        "weekly_review": (
            "Review my week using the last-7-day and last-28-day data below: what went well, what did not (load, "
            "intensity, recovery, sleep), and what to change next week given the upcoming events and agreed rules."),
        "plan_week": (
            "Plan the next 7 days, day by day (session, duration, intensity), respecting my availability, "
            "constraints, the agreed rules and the phase towards the next event. Start from the current load and "
            "recovery below and explain the logic of the week briefly."),
        "race_readiness": (
            "Assess how I am heading into «{event}»: current phase, what to do until race day (taper included), "
            "expected form against the goal and my past results. Be explicit about uncertainty: these metrics are "
            "local estimates, not Garmin's proprietary ones."),
        "race_none": "There are no future events in events.yaml: tell me and ask which race to prepare for.",
        "explain_metric": (
            "Explain the metric «{name}» simply: what it measures, how it is computed here (definition below), how "
            "to read my current value in the brief, and its limits. No formulas unless needed."),
        "answer": "Answer in {language}, directly and concretely.",
    },
}


class Prompts:
    def __init__(self, service: CoachService) -> None:
        self.service = service

    def _lang(self) -> tuple[dict[str, str], str]:
        lang = load_inputs(self.service.app.paths).athlete.advice_language or "it"
        return _TEXT.get(lang, _TEXT["en"]), lang

    def _compose(self, intro: str, aliases: tuple[str, ...], extra: str = "") -> str:
        text, lang = self._lang()
        packs, _ = self.service.pack_texts(aliases)
        body = "\n\n---\n\n".join(packs.values())
        answer = text["answer"].format(language=lang)
        return f"{intro}\n\n{answer}\n\n---\n\n{extra}{body}"

    def daily_checkin(self) -> str:
        text, _ = self._lang()
        return self._compose(text["daily_checkin"], ("daily", "athlete", "events"))

    def weekly_review(self) -> str:
        text, _ = self._lang()
        return self._compose(text["weekly_review"], ("weekly", "events"))

    def plan_week(self) -> str:
        text, _ = self._lang()
        return self._compose(text["plan_week"], ("daily", "athlete", "events", "7d"))

    def race_readiness(self, event: str = "") -> str:
        text, _ = self._lang()
        inputs = load_inputs(self.service.app.paths)
        today = self.service._today()
        future = [e for e in inputs.events if e.date >= today]
        wanted = event.strip().lower()
        match = next((e for e in future if wanted and wanted in e.name.lower()), None)
        if match is None:
            match = next((e for e in future if (e.priority or "").upper() == "A"), future[0] if future else None)
        if match is None:
            return self._compose(text["race_none"], ("events",))
        return self._compose(text["race_readiness"].format(event=f"{match.name} ({match.date})"),
                             ("events", "28d", "athlete", "daily"))

    def explain_metric(self, name: str) -> str:
        text, _ = self._lang()
        section = metric_section(name)
        extra = f"## Definition (docs/METRICS.md)\n\n{section}\n\n---\n\n" if section else ""
        return self._compose(text["explain_metric"].format(name=name), ("daily",), extra)


def metric_section(name: str, doc: Path = METRICS_DOC) -> str | None:
    """The docs/METRICS.md section explaining ``name`` (alias table, then heading match), or None."""
    if not doc.exists():
        return None
    sections = {m.group(1).strip(): m.group(2).strip() for m in
                re.finditer(r"^## (.+?)\n(.*?)(?=^## |\Z)", doc.read_text(encoding="utf-8"), re.M | re.S)}
    key = name.strip().lower()
    heading = METRIC_SECTIONS.get(key) or next((h for h in sections if key and key in h.lower()), None)
    if heading is None or heading not in sections:
        return None
    return f"### {heading}\n\n{sections[heading]}"
