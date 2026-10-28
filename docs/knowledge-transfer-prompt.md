# Prompt di passaggio di consegne (da incollare nella chat Copilot/ChatGPT esistente)

Risultato atteso: quattro blocchi di codice da salvare come `athlete.yaml`, `events.yaml`, `history.md`, `coach-notes.md`
nella cartella `<app data>/config/` di garmin-coach (vedi PLAN.md §5.1 e §11).

---

In questa conversazione sei stato il mio consulente per gli allenamenti. Sto passando a un nuovo coach che riceverà i miei dati Garmin in automatico, quindi NON deve ricevere numeri giornalieri, screenshot o tabelle di allenamenti: deve ricevere solo quello che i dati Garmin non possono dire.

Prepara un documento di passaggio di consegne composto ESATTAMENTE da quattro blocchi di codice, nell'ordine e nei formati indicati qui sotto. Non aggiungere testo fuori dai blocchi. Se non conosci un valore scrivi `null` (nei file YAML) oppure "non noto" (nei file Markdown): non inventare. Usa date nel formato AAAA-MM-GG. Scrivi in italiano, tranne le chiavi YAML che devono restare come nel modello.

BLOCCO 1 — file `athlete.yaml` (YAML valido, mantieni le chiavi esatte):

```yaml
name: ""
birth_year: null
sex: null            # M | F
height_cm: null
sports: []           # es. [running, cycling]
max_hr: null         # bpm, solo se lo abbiamo mai stabilito insieme
lthr: null           # soglia lattacida in bpm, se nota
ftp: null            # watt, se nota
constraints: []      # vincoli permanenti, una frase ciascuno, es. "ginocchio sx: niente lunghi > 25 km"
availability:        # minuti disponibili per giorno, 0 se mai
  mon: null
  tue: null
  wed: null
  thu: null
  fri: null
  sat: null
  sun: null
advice_language: it
units: metric
```

BLOCCO 2 — file `events.yaml` (YAML valido; elenca tutti gli eventi futuri di cui abbiamo parlato, dal più vicino):

```yaml
events:
  - name: ""
    date: AAAA-MM-GG
    sport: ""          # running | cycling | triathlon | trail | altro
    distance_km: null
    elevation_m: null
    goal: ""           # es. "sub 1:45" oppure "finire bene"
    priority: A        # A = obiettivo principale, B = importante, C = allenamento/test
    notes: ""
```

BLOCCO 3 — file `history.md` (Markdown, massimo 1200 parole, solo fatti, niente dati giornalieri):

```markdown
# Storia atletica

## Background
- Da quando pratico ogni sport, livello raggiunto, record personali precedenti al periodo con Garmin (distanza, tempo, anno).

## Infortuni e limiti
- Un punto per infortunio: data o periodo, cosa, quanto tempo fermo, come è stato risolto, cosa evitare oggi.

## Cosa ha funzionato / cosa no
- Struttura settimanale tipica che ha dato risultati e in quale periodo.
- Carichi, tipi di sessione o periodizzazioni che hanno portato a stanchezza, infortuni o stallo.
- Risposta al volume, all'intensità, al recupero, al caldo/freddo, all'altitudine se noto.

## Eventi passati
| Data | Evento | Distanza | Risultato | Note |
|---|---|---|---|---|

## Contesto di vita rilevante
- Lavoro, trasferte, famiglia, sonno: solo ciò che influenza la programmazione.

## Domande aperte
- Cose che avresti voluto sapere di me e non hai mai saputo.
```

BLOCCO 4 — file `coach-notes.md` (Markdown, una riga per regola, ogni riga inizia con la data in cui l'abbiamo concordata o, se non la ricordi, con la data di oggi):

```markdown
- AAAA-MM-GG <regola o preferenza concordata, in una frase>
```

Includi nel blocco 4: come preferisco ricevere i consigli (formato, lunghezza, tono), regole fisse sulla settimana (es. giorno del lungo, giorni di riposo), soglie o segnali su cui abbiamo deciso di intervenire (es. "se HRV sotto baseline per 3 giorni, giornata facile"), decisioni prese sulla preparazione degli eventi futuri, e qualsiasi accordo su cosa NON propormi.

Controlla prima di rispondere: i due YAML devono essere validi, nessun dato giornaliero, nessuna invenzione, quattro blocchi e nient'altro.
