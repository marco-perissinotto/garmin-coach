"""Sentences for the athlete, from the dashboard's numbers (docs/DASHBOARD.md).

Rule-based and transparent: every sentence quotes the numbers it rests on and how many sessions or nights they come
from. Italian, like the packs (advice_language). Four groups: where the athlete is strong, where they are declining,
what to focus on, what to keep an eye on; plus "lo sapevi?" patterns from their own history and a one-line summary.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import Any

from ..context import fmt

GROUPS = ("strong", "down", "focus", "watch")
MAX_PER_GROUP = 4
FEW = 4  # fewer sessions than this in the recent window: say the trend still needs confirming
SPORT_IT = {"run": "corsa", "bike": "bici", "swim": "nuoto"}
WEEKDAY_NIGHT = ("tra domenica e lunedì", "tra lunedì e martedì", "tra martedì e mercoledì",
                 "tra mercoledì e giovedì", "tra giovedì e venerdì", "tra venerdì e sabato", "tra sabato e domenica")
TRI_SHARE = {"swim": 0.10, "bike": 0.52, "run": 0.38}  # typical share of a 70.3 / Ironman finishing time


# -- formatting -----------------------------------------------------------------------------------------------------
def n_it(v: float | None, nd: int = 0, signed: bool = False) -> str:
    if v is None:
        return "–"
    s = f"{v:+,.{nd}f}" if signed else f"{v:,.{nd}f}"
    return s.replace(",", " ").replace(".", ",").replace("-", "−")


def pct_it(v: float | None, nd: int = 0, signed: bool = True) -> str:
    return "–" if v is None else n_it(v, nd, signed) + "%"


def hms(seconds: float | None) -> str:
    if not seconds:
        return "–"
    s = round(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def pace_km(seconds: float | None) -> str:
    return "–" if not seconds else f"{hms(seconds)}/km"


def pace_100(seconds: float | None) -> str:
    return "–" if not seconds else f"{hms(seconds)}/100 m"


def ddmm(iso: str | None) -> str:
    return fmt.ddmm(iso) if iso else "–"


def _few(n: int) -> str:
    return f" (solo {n} uscite: da confermare)" if n < FEW else ""


def _item(group: str, area: str, title: str, text: str, weight: float, *, ref: str | None = None) -> dict[str, Any]:
    return {"group": group, "area": area, "title": title, "text": text, "weight": round(weight, 2), "ref": ref}


# -- the four groups ------------------------------------------------------------------------------------------------
def _trend_items(label: str, area: str, t: dict[str, Any], what: str, unit_fmt: Any, ref: str) -> list[dict[str, Any]]:
    """One strong/down item from a sport trend (``what`` explains the metric in words)."""
    if not t.get("verdict") or t["verdict"] == "flat":
        return []
    yoy = t.get("yoy_pct")
    yoy_s = f"; rispetto allo stesso periodo di un anno fa {pct_it(yoy, 1)}" if yoy is not None else ""
    text = (f"{what}: {unit_fmt(t['recent'])} negli ultimi 42 giorni contro {unit_fmt(t['previous'])} nei 3 mesi "
            f"prima ({pct_it(t['change_pct'], 1)}){yoy_s}. Su {t['n_recent']} uscite{_few(t['n_recent'])}.")
    weight = abs(t["better_pct"]) * (0.5 if t["n_recent"] < FEW else 1.0) + 1
    if t["verdict"] == "up":
        return [_item("strong", area, f"{label} in crescita", text, weight, ref=ref)]
    return [_item("down", area, f"{label} in calo", text, weight, ref=ref)]


def _run(d: dict[str, Any], out: list[dict[str, Any]]) -> None:
    run = d["run"]
    t = run["efficiency"]["trend"]
    if t.get("verdict"):
        ep = run["easy_pace"]
        extra = (f" Ritmo medio delle corse facili {pace_km(ep['recent'])} a {n_it(ep['hr']['recent'])} bpm "
                 f"(prima {pace_km(ep['previous'])} a {n_it(ep['hr']['previous'])} bpm).") if ep["recent"] else ""
        for it in _trend_items("Corsa: efficienza aerobica", "Corsa", t, "Metri percorsi per battito nelle corse "
                               "facili", lambda v: n_it(v, 2), "run"):
            it["text"] += extra
            out.append(it)
    for b in run["bests"]:
        best = b["best"]
        if not best or b["recent"] is None or b["recent"] > best["s"]:
            continue
        if (b.get("best_km") or b["km"]) > b["km"] * 1.1:
            continue  # a pass inside a longer run: in the table, not a headline
        prev = [v["s"] for y, v in b["years"].items() if y != best["date"][:4]]
        gain = f", {hms(min(prev) - best['s'])} meglio del record precedente" if prev and min(prev) > best["s"] else ""
        out.append(_item("strong", "Corsa", f"Record personale: {b['label']}",
                         f"{hms(best['s'])} il {ddmm(best['date'])} ({best['name']}){gain}.", 8, ref="run"))
    dyn = {x["key"]: x for x in run["dynamics"]}
    better = []
    if (c := dyn.get("cadence_spm")) and c["delta"] is not None and c["delta"] >= 3:
        better.append(f"cadenza {n_it(c['recent'])} passi/min ({n_it(c['delta'], 0, True)})")
    if (c := dyn.get("stance_time_ms")) and c["delta"] is not None and c["delta"] <= -4:
        better.append(f"contatto col suolo {n_it(c['recent'])} ms ({n_it(c['delta'], 0, True)})")
    if (c := dyn.get("vertical_ratio_pct")) and c["delta"] is not None and c["delta"] <= -0.2:
        better.append(f"rapporto verticale {n_it(c['recent'], 1)}% ({n_it(c['delta'], 1, True)})")
    if better:
        n = dyn["cadence_spm"]["n_recent"] if "cadence_spm" in dyn else 0
        out.append(_item("strong", "Corsa", "Tecnica di corsa più economica",
                         "Nelle corse facili, ultimi 42 giorni contro i 3 mesi prima: " + ", ".join(better)
                         + f". Su {n} uscite.", 3 + len(better), ref="run"))
    dec = run["decoupling"]
    if dec["recent"] is not None and dec["n_recent"] >= 3 and dec["recent"] >= 5 and (dec["delta"] or 0) >= 2:
        out.append(_item("down", "Corsa", "Più deriva cardiaca nelle corse lunghe",
                         f"Nelle uscite oltre l'ora la frequenza sale del {pct_it(dec['recent'], 1, False)} a parità "
                         f"di ritmo (prima {pct_it(dec['previous'], 1, False)}). Su {dec['n_recent']} uscite.",
                         4, ref="run"))
    v = run["volume"]
    if v["ramp_pct"] is not None and v["ramp_pct"] >= 30 and v["km_7"] >= 20:
        out.append(_item("watch", "Corsa", "Volume di corsa in salita rapida",
                         f"{n_it(v['km_7'], 1)} km negli ultimi 7 giorni contro {n_it(v['km_week_before'], 1)} di "
                         f"media nelle 4 settimane prima ({pct_it(v['ramp_pct'])}). I tessuti si adattano più "
                         "lentamente del fiato.", 7, ref="run"))


def _bike(d: dict[str, Any], out: list[dict[str, Any]]) -> None:
    bike = d["bike"]
    for key, where in (("efficiency_outdoor", "su strada"), ("efficiency_indoor", "sui rulli")):
        out += _trend_items(f"Bici {where}: efficienza", "Bici", bike[key]["trend"],
                            f"Watt per battito nelle uscite facili {where}", lambda v: n_it(v, 2), "bike")
    long = [p for p in bike["power"] if p["seconds"] >= 1200 and p["pct_of_year"] is not None]
    low = [p for p in long if p["pct_of_year"] < 92]
    if low:
        parts = [f"{p['label']} {p['recent']['w']} W, {p['pct_of_year']}% del massimo dell'anno "
                 f"({p['year']['w']} W il {ddmm(p['year']['date'])})" for p in low]
        out.append(_item("down", "Bici", "Potenza di durata sotto i tuoi livelli",
                         "Migliori potenze degli ultimi 42 giorni: " + "; ".join(parts) + ".",
                         4 + (92 - min(p["pct_of_year"] for p in low)) / 2, ref="bike"))
    ftp = bike["ftp"]
    if ftp.get("set") and ftp.get("estimate") and ftp["estimate"] < ftp["set"] * 0.93:
        out.append(_item("focus", "Bici", "Verifica l'FTP",
                         f"Le zone di potenza usano {ftp['set']} W, ma il miglior 20' degli ultimi 90 giorni "
                         f"({ftp['from_w']} W, {ddmm(ftp['date'])}) vale circa {ftp['estimate']} W. Se non era uno "
                         "sforzo massimale, un test di 20' lo chiarisce; se lo era, le zone sono troppo alte e i "
                         "lavori «in Z2» diventano medi.", 7, ref="bike"))
    dec = bike["decoupling"]
    if dec["recent"] is not None and dec["n_recent"] >= 2 and dec["previous"] is not None and dec["delta"] <= -2:
        out.append(_item("strong", "Bici", "Tenuta aerobica nelle uscite lunghe",
                         f"Deriva tra potenza e frequenza nelle uscite oltre 1h30: {pct_it(dec['recent'], 1, False)} "
                         f"contro {pct_it(dec['previous'], 1, False)} nei 3 mesi prima. Su {dec['n_recent']} "
                         f"uscite{_few(dec['n_recent'])}.", 4, ref="bike"))
    elif dec["recent"] is not None and dec["n_recent"] >= 2 and dec["recent"] >= 5 and (dec["delta"] or 0) >= 2:
        out.append(_item("down", "Bici", "Più deriva nelle uscite lunghe",
                         f"Deriva tra potenza e frequenza {pct_it(dec['recent'], 1, False)} contro "
                         f"{pct_it(dec['previous'], 1, False)}. Su {dec['n_recent']} uscite.", 4, ref="bike"))


def _swim(d: dict[str, Any], out: list[dict[str, Any]]) -> None:
    sw = d["swim"]
    items = _trend_items("Nuoto: passo", "Nuoto", sw["pace"]["trend"], "Passo medio in piscina", pace_100, "swim")
    if items and (s := sw["swolf"])["recent"] is not None and s["previous"] is not None:
        items[0]["text"] += f" Swolf {n_it(s['recent'])} (prima {n_it(s['previous'])})."
    out += items


def _load(d: dict[str, Any], out: list[dict[str, Any]]) -> None:
    h = d["hero"]
    if not h:
        return
    if h["ctl_28"] and (ch := (h["ctl"] / h["ctl_28"] - 1) * 100) <= -6:
        why = " Dopo una gara è il recupero che serve; se dura più di 3–4 settimane si perde base." if (
            d["recent_race"]) else ""
        out.append(_item("down", "Forma", "Forma in calo",
                         f"Carico cronico {n_it(h['ctl'])} contro {n_it(h['ctl_28'])} di 4 settimane fa "
                         f"({pct_it(ch)}).{why}", 3 + abs(ch) / 3, ref="load"))
    elif h["ctl_28"] and (ch := (h["ctl"] / h["ctl_28"] - 1) * 100) >= 6:
        out.append(_item("strong", "Forma", "Forma in crescita",
                         f"Carico cronico {n_it(h['ctl'])} contro {n_it(h['ctl_28'])} di 4 settimane fa "
                         f"({pct_it(ch)}).", 3 + ch / 3, ref="load"))
    if h["ctl_365"] and abs(yy := (h["ctl"] / h["ctl_365"] - 1) * 100) >= 10:
        grp, word = ("strong", "sopra") if yy > 0 else ("down", "sotto")
        out.append(_item(grp, "Forma", f"Forma {word} un anno fa",
                         f"Carico cronico {n_it(h['ctl'])} oggi, {n_it(h['ctl_365'])} un anno fa ({pct_it(yy)}).",
                         2 + abs(yy) / 6, ref="load"))
    if h["acwr"] and h["acwr"] > 1.5:
        out.append(_item("watch", "Carico", "Picco di carico",
                         f"Ultimi 7 giorni a {n_it(h['acwr'], 2)} volte la media delle 4 settimane: oltre 1,5 il "
                         "rischio di sovraccarico sale.", 9, ref="load"))
    if h["monotony"] and h["monotony"] >= 2:
        out.append(_item("watch", "Carico", "Settimana troppo uniforme",
                         f"Monotonia {n_it(h['monotony'], 1)}: alterna giorni duri e facili.", 5, ref="load"))


def _intensity(d: dict[str, Any], out: list[dict[str, Any]]) -> None:
    it = d["intensity"]
    phase = (d["target"] or {}).get("phase")
    if not it or phase not in ("base", "costruzione"):
        return
    now, prev = it["now"], it["previous"]
    th = it["thresholds"]
    if now["mid"] >= 0.20:
        was = f" (nelle 4 settimane prima {pct_it(prev['mid'] * 100, 0, False)})" if prev else ""
        run_band = f"; nella corsa {th['run'][0]}–{th['run'][1]} bpm" if th.get("run") else ""
        bike_band = f", in bici {th['bike'][0]}–{th['bike'][1]} bpm" if th.get("bike") else ""
        out.append(_item("focus", "Intensità", "Troppa «zona grigia» per la fase di " + phase,
                         f"Il {pct_it(now['mid'] * 100, 0, False)} del tempo delle ultime 4 settimane è tra la soglia "
                         f"aerobica e quella anaerobica{was}{run_band}{bike_band}. In {phase} conviene stare sotto il "
                         "15%: facile davvero facile, e poche sedute dure ma dure.", 4 + (now["mid"] - 0.2) * 40,
                         ref="intensity"))


def _event(d: dict[str, Any], out: list[dict[str, Any]]) -> None:
    ev = d["event"]
    if not ev:
        return
    past = [s for s in ev["series"] if not s["current"]]
    cur = next(s for s in ev["series"] if s["current"])["same_point"]
    if not past or cur["ctl"] is None:
        return
    then = [s["same_point"] for s in past if s["same_point"]["ctl"] is not None]
    if not then:
        return
    names = ", ".join(f"{n_it(s['same_point']['ctl'])} nel {s['label']}" for s in past
                      if s["same_point"]["ctl"] is not None)
    best = max(then, key=lambda s: s["ctl"])
    if cur["ctl"] >= best["ctl"]:
        out.append(_item("strong", "Obiettivo", f"Verso {ev['event']}: parti più avanti",
                         f"A {ev['days']} giorni dalla gara il tuo carico cronico è {n_it(cur['ctl'])}, contro "
                         f"{names} allo stesso punto.", 7, ref="event"))
    elif cur["ctl"] < min(s["ctl"] for s in then):
        out.append(_item("down", "Obiettivo", f"Verso {ev['event']}: parti più indietro",
                         f"A {ev['days']} giorni dalla gara il tuo carico cronico è {n_it(cur['ctl'])}, contro "
                         f"{names} allo stesso punto.", 6, ref="event"))
    # the discipline with the largest gap against the previous seasons at this point
    tri = (ev.get("sport") or "").lower() == "triathlon"
    gaps = []
    for k in ("bike", "run", "swim"):
        before = sum(s[f"h_{k}"] for s in then) / len(then)
        if before >= 0.5 and cur[f"h_{k}"] < before * 0.85:
            gaps.append((k, cur[f"h_{k}"], before, (before - cur[f"h_{k}"]) * (TRI_SHARE[k] if tri else 1)))
    if gaps:
        k, now_h, before_h, _ = max(gaps, key=lambda g: g[3])
        share = (f" Nella gara la {SPORT_IT[k]} vale circa il {round(TRI_SHARE[k] * 100)}% del tempo totale."
                 if tri else "")
        out.append(_item("focus", SPORT_IT[k].capitalize(), f"{SPORT_IT[k].capitalize()}: la disciplina con più "
                         f"margine verso {ev['event']}",
                         f"Nelle ultime 4 settimane {n_it(now_h, 1)} h a settimana, contro {n_it(before_h, 1)} di "
                         f"media nelle stagioni passate allo stesso punto.{share}", 8, ref="event"))


def _health(d: dict[str, Any], out: list[dict[str, Any]]) -> None:
    he = d["health"]
    if he["alarm"]:
        a = he["alarm"]
        out.append(_item("watch", "Salute", "FC a riposo alta e HRV bassa per 2 giorni",
                         f"FC a riposo {a['rhr']} bpm (di solito {a['rhr_base']}) e HRV {a['hrv']} ms sotto il tuo "
                         "intervallo normale: segnale tipico di malattia in arrivo o di carico non assorbito. Oggi "
                         "solo facile o riposo.", 10, ref="health"))
    for m in he["metrics"]:
        if m["status"] not in ("good", "watch"):
            continue
        unit = f" {m['unit']}" if m["unit"] and m["unit"] != "%" else m["unit"]
        nd = m["decimals"]
        text = (f"Media degli ultimi 7 giorni {n_it(m['now'], nd)}{unit} contro {n_it(m['base'], nd)}{unit} dei 3 "
                f"mesi prima ({n_it(m['delta'], max(nd, 1), True)}).")
        if m["status"] == "good":
            out.append(_item("strong", "Salute", f"{m['label']} migliore del solito", text, 2 + abs(m["z"]),
                             ref="health"))
        else:
            out.append(_item("watch", "Salute", f"{m['label']} peggiore del solito", text, 4 + abs(m["z"]) * 2,
                             ref="health"))
    s = he["sleep"]
    if s and s["debt_14_h"] >= 4:
        out.append(_item("watch", "Sonno", "Debito di sonno",
                         f"Nelle ultime 14 notti {n_it(s['debt_14_h'], 1)} h sotto il fabbisogno che stima Garmin.",
                         5 + s["debt_14_h"] / 2, ref="sleep"))
    if s and (s["bedtime_sd_min"] or 0) >= 45:
        out.append(_item("focus", "Sonno", "Orari di sonno irregolari",
                         f"Vai a letto in media alle {s['bedtime']}, ma con oscillazioni di ±{n_it(s['bedtime_sd_min'])}"
                         f" min (sveglia alle {s['wake']}, ±{n_it(s['wake_sd_min'])} min). Orari regolari valgono "
                         "quanto un'ora di sonno in più per il recupero.", 3 + (s["bedtime_sd_min"] - 45) / 10,
                         ref="sleep"))


def _misc(d: dict[str, Any], out: list[dict[str, Any]]) -> None:
    st = d["strength"]
    multi = (d["target"] or {}).get("sport") in ("triathlon", "duathlon", "multisport")
    if multi and st["sessions_28"] == 0:
        since = f" (ultima il {ddmm(st['last'])})" if st["last"] else ""
        out.append(_item("focus", "Forza", "Nessuna seduta di forza nelle ultime 4 settimane",
                         f"{st['sessions_90']} sedute negli ultimi 3 mesi{since}. In fase di base 2 sedute brevi a "
                         "settimana proteggono tendini e tibie quando il volume sale.", 3, ref="strength"))
    c = d["consistency"]
    if c["active_days"] >= 22:
        out.append(_item("strong", "Costanza", "Costanza",
                         f"Ti sei allenato {c['active_days']} giorni su {c['days']}.", 2, ref="load"))
    for sym in ((d.get("state") or {}).get("symptoms") or []):
        out.append(_item("watch", "Sintomi", "Sintomo segnalato al coach",
                         f"{sym['text']}" + (f" (dal {ddmm(sym.get('since'))})" if sym.get("since") else ""),
                         8, ref="plan"))


# -- "lo sapevi?" ---------------------------------------------------------------------------------------------------
def patterns(d: dict[str, Any]) -> list[dict[str, Any]]:
    p, out = d["patterns"], []
    if h := p.get("hrv_after_load"):
        back = (" e torna normale la seconda" if h["hard_2"] is not None and abs(h["hard_2"]) < 1.5 else
                f" e la seconda è ancora a {n_it(h['hard_2'], 1, True)} ms" if h["hard_2"] is not None else "")
        out.append({"title": "Quanto ti costa un giorno duro",
                    "text": f"La notte dopo un giorno con carico sopra {h['hard_from']} la tua HRV è in media "
                            f"{n_it(h['hard_1'], 1, True)} ms rispetto alla settimana prima{back}. Dopo un giorno di "
                            f"riposo è {n_it(h['rest_1'], 1, True)} ms. Su {h['n_hard']} giorni duri e "
                            f"{h['n_rest']} di riposo nell'ultimo anno.",
                    "ref": "health"})
    if ls := p.get("late_sessions"):
        diff = ls["score_late"] - ls["score_early"]
        verdict = ("non ti peggiora il sonno" if diff >= -3 else "ti costa sonno")
        out.append({"title": "Allenarti la sera",
                    "text": f"Dopo un allenamento finito dopo le 19:30 {verdict}: punteggio {n_it(ls['score_late'])} "
                            f"e HRV {n_it(ls['hrv_late'])} ms, contro {n_it(ls['score_early'])} e "
                            f"{n_it(ls['hrv_early'])} ms quando finisci prima delle 17. Su {ls['n_late']} sere e "
                            f"{ls['n_early']} pomeriggi.",
                    "ref": "sleep"})
    if wd := p.get("sleep_by_weekday"):
        avg = sum(wd) / 7
        lo = min(range(7), key=lambda i: wd[i])
        if avg - wd[lo] >= 0.3:
            out.append({"title": "La notte più corta",
                        "text": f"La notte {WEEKDAY_NIGHT[lo]} dormi in media {n_it(wd[lo], 1)} h, "
                                f"{n_it((avg - wd[lo]) * 60)} minuti meno della tua media ({n_it(avg, 1)} h), negli "
                                "ultimi 6 mesi.",
                        "ref": "sleep"})
    y = d["years"]
    years = y["years"]
    if len(years) >= 2:
        this, last = y["to_date"][years[-1]], y["to_date"][years[-2]]
        h_this = sum(v["hours"] for v in this.values())
        h_last = sum(v["hours"] for v in last.values())
        if h_last:
            parts = []
            for k in ("bike", "run", "swim"):
                if last[k]["km"] >= 20:
                    parts.append(f"{SPORT_IT[k]} {pct_it((this[k]['km'] / last[k]['km'] - 1) * 100)} km")
            out.append({"title": f"Il {years[-1]} fin qui",
                        "text": f"{n_it(h_this)} ore di allenamento al {ddmm(d['today'])}, contro {n_it(h_last)} "
                                f"nel {years[-2]} alla stessa data ({pct_it((h_this / h_last - 1) * 100)}): "
                                + ", ".join(parts) + ".",
                        "ref": "years"})
    return out


# -- assemble -------------------------------------------------------------------------------------------------------
def build(d: dict[str, Any]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for fn in (_run, _bike, _swim, _load, _intensity, _event, _health, _misc):
        fn(d, items)
    groups: dict[str, list[dict[str, Any]]] = {g: [] for g in GROUPS}
    for it in sorted(items, key=lambda x: -x["weight"]):
        if len(groups[it["group"]]) < MAX_PER_GROUP:
            groups[it["group"]].append(it)
    return {"groups": groups, "patterns": patterns(d), "summary": summary(d, groups)}


def _area(items: Iterable[dict[str, Any]], skip: tuple[str, ...] = ()) -> str | None:
    return next((it["area"] for it in items if it["area"] not in skip), None)


def summary(d: dict[str, Any], groups: dict[str, list[dict[str, Any]]]) -> str:
    """One or two sentences at the top of the page."""
    h = d["hero"]
    if not h:
        return "Non ci sono ancora abbastanza dati per un quadro: carica lo storico (garmin-coach backfill)."
    rank = round(h["rank"] * 100)
    since = h["history_from"][:4]
    tsb = h["form"]
    fresh = ("molto fresco" if tsb is not None and tsb > 0.15 else "fresco" if tsb is not None and tsb > -0.05 else
             "un po' affaticato" if tsb is not None and tsb > -0.30 else "affaticato" if tsb is not None else None)
    s = f"Forma {n_it(h['ctl'])}: più alta del {rank}% dei tuoi giorni dal {since}"
    if fresh:
        s += f", e oggi sei {fresh}"
    r = (h.get("readiness") or {}).get("score")
    if r is not None:
        s += f" (prontezza {r})"
    s += "."
    strong = _area(groups["strong"], ("Salute", "Costanza"))
    focus = _area(groups["focus"]) or _area(groups["down"])
    if strong and focus and strong != focus:
        s += f" Il punto forte del momento: {strong.lower()}. Dove c'è più margine: {focus.lower()}."
    elif focus:
        s += f" Dove c'è più margine: {focus.lower()}."
    if groups["watch"]:
        s += f" Da tenere d'occhio: {groups['watch'][0]['title'].lower()}."
    return s


def today_label(today: date) -> str:
    months = ("gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre",
              "ottobre", "novembre", "dicembre")
    days = ("lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica")
    return f"{days[today.weekday()]} {today.day} {months[today.month - 1]} {today.year}"
