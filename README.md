# garmin-coach

Un coach sportivo personale basato su Claude, che conosce i tuoi dati Garmin senza che tu debba incollare
screenshot.

`garmin-coach` è un programma che gira **solo sul tuo computer**. Scarica i tuoi dati da Garmin Connect
(attività, sonno, HRV, frequenza cardiaca, Body Battery, stress, peso, zone cardiache), li tiene in una cache
locale e li mette a disposizione di Claude Desktop e Claude Code tramite un server MCP. Tu chiedi a Claude
"come mi alleno oggi?" e Claude risponde conoscendo la notte appena passata, le ultime settimane di carico, la tua
storia atletica e le regole di coaching che avete concordato.

## Come funziona

```
 Claude Desktop / Claude Code ──► server MCP (garmin-coach mcp) ──┐
 riga di comando (garmin-coach context ...) ──────────────────────┤
                                                                  ▼
                                                      "i dati sono abbastanza freschi?"
                                                         │ no                 │ sì
                                                         ▼                    │
 Garmin Connect  ◄── sola lettura ──  sincronizzazione ──► cache SQLite ◄──────┘
                                                                  │
                                                                  ▼
                                                    metriche + "context pack" per il coach
```

Tre idee alla base:

1. **La cache si aggiorna da sola, quando serve.** Non c'è nessun programma in esecuzione in background, nessuna
   pianificazione da installare. Ogni volta che Claude chiede un dato, il programma controlla quanto è vecchia la
   cache e, se ha superato la sua scadenza, la aggiorna prima di rispondere. Al primo avvio su un computer vuoto il
   caricamento dello storico parte da solo.
2. **I dati restano tuoi.** Tutto vive in cartelle locali non sincronizzate con OneDrive o iCloud. Il programma fa
   solo letture verso Garmin, mai scritture. Nessun servizio esterno, nessuna telemetria.
3. **Il coach è Claude, non il programma.** Il programma prepara i numeri in modo deterministico; l'interpretazione e
   i consigli li dà Claude, che riceve anche la tua storia (`history.md`) e le regole concordate (`coach-notes.md`).

## Stato del progetto

| Fase | Contenuto | Stato |
|---|---|---|
| 0 | Sicurezza, struttura del progetto, comando `doctor` | fatta |
| 1 | Sincronizzazione, cache, caricamento dello storico, aggiornamento su richiesta, lavori in background | fatta |
| 2 | Metriche (carico, forma, prontezza) e "context pack" per il coach | fatta |
| 3 | Server MCP completo (tool, risorse, prompt), integrazione con Claude Desktop e Claude Code | da fare |
| 4 | Secondo computer (Mac), avviso di scadenza della chiave, rifiniture | da fare |
| 5–6 | Opzionali: file FIT, nutrizione, dashboard locale | da fare |

Il piano completo è in [docs/PLAN.md](docs/PLAN.md); le decisioni prese durante l'implementazione, con le
motivazioni, in [docs/DECISIONS.md](docs/DECISIONS.md).

## Requisiti

- Un account Garmin Connect con una **chiave Personal API** (Garmin Connect → Impostazioni → Personal API). La chiave
  è di sola lettura e dura fino a un anno.
- Python 3.12 o superiore e [uv](https://docs.astral.sh/uv/).
- Windows 11 o macOS. Claude Desktop o Claude Code per usare il coach (dalla Fase 3).

## Installazione

```sh
# 1. clona il repo in una cartella NON sincronizzata con OneDrive/iCloud (niente Desktop o Documenti sincronizzati)
git clone https://github.com/marco-perissinotto/garmin-coach.git
cd garmin-coach
git config core.hooksPath .githooks      # blocca i commit che contengono una chiave o un token

# 2. installa le dipendenze
uv sync

# 3. salva la chiave Personal API nel file previsto dal client ufficiale Garmin
#    Windows: %USERPROFILE%\.config\garmin\connect-personal-api-key
#    macOS:   ~/.config/garmin/connect-personal-api-key   (poi: chmod 600 su quel file)

# 4. verifica
uv run garmin-coach doctor
```

`doctor` controlla la chiave, prova lo scambio del token con Garmin, apre il database, verifica i file personali e
stampa il comando successivo da lanciare.

## Dove stanno i dati

| Cosa | Windows | macOS |
|---|---|---|
| Chiave API e token | `%USERPROFILE%\.config\garmin\` | `~/.config/garmin/` |
| Cache, log, configurazione personale | `%LOCALAPPDATA%\garmin-coach\` | `~/Library/Application Support/garmin-coach/` |

La variabile d'ambiente `GARMIN_COACH_HOME` sposta la cartella dati dove preferisci. Nessuno di questi percorsi è
sincronizzato dal cloud, e niente di tutto questo finisce nel repo.

## I tuoi file personali

Vanno nella cartella `<dati>/config/`. I modelli sono in [config/](config/):

| File | Contenuto |
|---|---|
| `athlete.yaml` | Profilo: età, altezza, peso, sport, FC massima, soglia, FTP, vincoli, disponibilità settimanale |
| `events.yaml` | Le gare in programma: data, sport, distanza, obiettivo, priorità |
| `history.md` | La storia atletica che Garmin non può sapere: infortuni, cosa ha funzionato, gare passate |
| `coach-notes.md` | Le regole di coaching concordate, una riga per regola con la data |
| `settings.yaml` | Opzionale: scadenze della cache, ritmo delle chiamate, giorni di storico da caricare |

Questa cartella è l'unica cosa da copiare tra un computer e l'altro: ogni computer tiene la propria cache e la
aggiorna per conto suo.

## Comandi

| Comando | Cosa fa |
|---|---|
| `garmin-coach doctor [--offline]` | Verifica chiave, connessione a Garmin, database e file personali |
| `garmin-coach backfill [--days N] [--background]` | Carica lo storico. Riprende da dove si era fermato, non scarica due volte la stessa cosa |
| `garmin-coach sync [--tier A,B] [--force] [--background]` | Aggiorna la cache. Senza `--force` rispetta le scadenze: se tutto è fresco non fa chiamate |
| `garmin-coach normalize --all` | Ricostruisce le tabelle dai dati grezzi salvati, senza contattare Garmin |
| `garmin-coach status [--watch] [--json]` | Età della cache per livello, storico caricato?, lavoro in corso con avanzamento e stima di fine |
| `garmin-coach jobs list \| show <id> \| cancel <id> \| prune` | Storico e controllo dei lavori di sincronizzazione |
| `garmin-coach context show [daily\|weekly\|events\|athlete\|recent\|instructions] [--offline]` | Stampa un pack pronto da incollare in una chat; senza `--offline` aggiorna prima i dati che servono |
| `garmin-coach context build [--offline]` | Ricalcola le metriche e riscrive tutti i pack in `<dati>/context/` |
| `garmin-coach context export --zip [--offline]` | Aggiorna tutto e impacchetta i pack in uno zip (per esempio per un Project di ChatGPT) |
| `garmin-coach mcp` | Avvia il server MCP per Claude. Se la cache è vuota, fa partire da solo il caricamento dello storico |

Codici di uscita di `backfill` e `sync`: 0 completato, 2 parziale (riprende al prossimo avvio), 1 fallito.

## Cosa legge il coach

Dopo ogni sincronizzazione il programma ricalcola le metriche e scrive in `<dati>/context/` dei "context pack" in
Markdown (più `context.json` con gli stessi dati strutturati):

| Pack | Contenuto |
|---|---|
| `daily-brief.md` | Oggi: segnali di attenzione, prontezza (euristica) con motivazioni, sonno, HRV, FC a riposo, Body Battery, attività di oggi e ieri, carico (ATL/CTL/TSB/ACWR), prossime gare |
| `athlete.md` | Profilo, soglie e zone, disponibilità, vincoli, più `history.md` e `coach-notes.md` per intero |
| `events.md` | Gare in programma con giorni mancanti e fase di preparazione |
| `last-7d.md`, `last-28d.md` | Tabelle giorno per giorno, settimane per sport, tempo in zona, indicatori di efficienza, potenze di picco, peso |
| `activities-recent.md` | Le ultime attività con i numeri principali |
| `coach-instructions.md` | Istruzioni brevi e senza dati personali da usare come istruzioni di un Project di Claude |

Le formule e i loro limiti sono in [docs/METRICS.md](docs/METRICS.md). Il VO2max non è disponibile con la Personal
API: al suo posto ci sono indicatori di efficienza (metri per battito in corsa, watt per battito in bici).

## Come si aggiorna la cache

I dati sono divisi in livelli, ognuno con la propria scadenza. Una richiesta aggiorna solo i livelli di cui ha
bisogno:

| Livello | Dati | Scadenza | Modo |
|---|---|---|---|
| A | Elenco attività, dettaglio delle attività recenti | 60 min | immediato, prima di rispondere |
| B | Oggi e ieri: riepilogo, sonno, HRV, frequenza cardiaca, Body Battery | 60 min | immediato |
| C | Ultimi 7 giorni di tutte le serie giornaliere (riallineamento) | 24 h | in background |
| D | Dati giornalieri opzionali (SpO2, nutrizione) | 24 h | in background |
| E | Profilo, zone cardiache, fitness age, obiettivo peso | 7 giorni | in background |
| H | Storico completo | una volta | automatico al primo avvio |

Protezioni verso Garmin: pausa minima tra le chiamate, nuovi tentativi con attesa crescente su errori temporanei,
e un interruttore che sospende le chiamate per un po' se Garmin segnala troppe richieste. In quel caso Claude riceve
i dati in cache con l'avviso che potrebbero non essere aggiornati.

## Sicurezza

- La chiave API non viene mai stampata, registrata nei log o salvata nel repo. I log mascherano automaticamente
  chiavi e token.
- Il programma usa il client Python ufficiale di Garmin per l'autenticazione e fa solo richieste di lettura.
- Il server MCP comunica solo con il processo di Claude sulla stessa macchina (stdio); non apre porte di rete.
- Il gancio pre-commit e un test rifiutano qualsiasi file che contenga una stringa simile a una chiave o a un token.

## Sviluppo

```sh
uv run pytest                                            # test offline: nessuna chiamata a Garmin
GARMIN_COACH_LIVE=1 uv run pytest tests/test_live.py     # unico test dal vivo (3 chiamate)
uv run ruff check src tests && uv run mypy               # stile e tipi
```

Il progetto è a uso personale: la Personal API di Garmin è pensata per un solo utente e non per applicazioni
multiutente.
