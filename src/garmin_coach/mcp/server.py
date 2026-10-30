"""MCP server (PLAN §9): tools, resources and prompts over stdio, or streamable HTTP bound to 127.0.0.1 only.

The server must start even with an empty cache, a missing key or Garmin down (PLAN §9.1): the startup check only
enqueues, and every tool answers with an ``error`` field instead of failing. Tool logic lives in ``tools.py``; this
module only registers it, with descriptions written for the model.
"""

from __future__ import annotations

import json
import logging
import secrets
import sys
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, ConfigDict, Field

from ..app import App
from ..sync.startup import startup_check
from .prompts import Prompts
from .tools import CoachService

log = logging.getLogger(__name__)

INSTRUCTIONS = """Local Garmin Connect data of one athlete (the user), cached on this machine and refreshed on demand.
Start a coaching conversation with get_daily_brief and get_athlete_profile; use get_events for planning. Every data
tool returns a `freshness` object: if `stale` is true or `history_loaded` is false, tell the user before advising.
Readiness is a local heuristic, not Garmin's Training Readiness. Units: distances in metres (or km when the field name
says so), durations in seconds (or as stated), heart rate in bpm, power in watts, HRV in ms. Pack texts are Italian."""

MaxAge = Annotated[int | None, Field(
    description="Maximum acceptable cache age in minutes. Omit for the default TTL; 0 forces a refresh from Garmin "
                "of the data this tool needs (use after the user says they just synced the watch).")]
DateArg = Annotated[str | None, Field(description="Calendar date YYYY-MM-DD (the athlete's local day). Default today.")]
StartArg = Annotated[str | None, Field(description="First day, YYYY-MM-DD (inclusive).")]
EndArg = Annotated[str | None, Field(description="Last day, YYYY-MM-DD (inclusive). Default today.")]

StateText = Annotated[str, Field(description="One short line, 3-300 characters.")]


class SymptomIn(BaseModel):
    """A symptom the athlete reported (never a measured value)."""

    text: StateText
    since: Annotated[str | None, Field(description="YYYY-MM-DD when it started, if known.")] = None
    noted: Annotated[str | None, Field(description="YYYY-MM-DD when it was reported. Default today.")] = None


class PlanDayIn(BaseModel):
    date: Annotated[str, Field(description="YYYY-MM-DD, within today - 7 .. today + 90 days.")]
    session: StateText


class PlanIn(BaseModel):
    """The plan agreed for the next days (at most 14) and the conditions that change it."""

    model_config = ConfigDict(validate_by_name=True, validate_by_alias=True)
    from_: Annotated[str | None, Field(alias="from", description="First day, YYYY-MM-DD.")] = None
    to: Annotated[str | None, Field(description="Last day, YYYY-MM-DD. Default: the last plan day.")] = None
    days: list[PlanDayIn] = []
    change_if: Annotated[list[str], Field(description="Conditions that change the plan, one line each (max 8).")] = []


class OpenDecisionIn(BaseModel):
    question: StateText
    decide_by: Annotated[str | None, Field(description="Deadline YYYY-MM-DD, within 90 days.")] = None
    noted: Annotated[str | None, Field(description="YYYY-MM-DD when it was raised. Default today.")] = None


class TakenDecisionIn(BaseModel):
    text: StateText
    date: Annotated[str | None, Field(description="YYYY-MM-DD when it was taken. Default today.")] = None


class DecisionsIn(BaseModel):
    """Decisions still open (max 8, with a deadline) and decisions taken (max 10)."""

    open: list[OpenDecisionIn] = []
    taken: list[TakenDecisionIn] = []


PACK_RESOURCES = {"daily-brief": "daily", "last-7d": "7d", "last-28d": "28d", "athlete": "athlete", "events": "events"}


class _StaticToken:
    """Bearer token check for the optional localhost HTTP mode (a random token printed at start)."""

    def __init__(self, token: str) -> None:
        self._token = token

    async def verify_token(self, token: str) -> Any:
        from mcp.server.auth.provider import AccessToken

        if secrets.compare_digest(token, self._token):
            return AccessToken(token=token, client_id="garmin-coach-local", scopes=[])
        return None


def build_server(app: App, *, http_token: str | None = None, port: int = 8765) -> MCPServer:
    service = CoachService(app)
    prompts = Prompts(service)
    kwargs: dict[str, Any] = {}
    if http_token:
        from mcp.server.auth.settings import AuthSettings
        from pydantic import AnyHttpUrl

        base = f"http://127.0.0.1:{port}"
        kwargs = {"token_verifier": _StaticToken(http_token),
                  "auth": AuthSettings(issuer_url=AnyHttpUrl(base), resource_server_url=AnyHttpUrl(f"{base}/mcp"))}
    mcp = MCPServer("garmin-coach", instructions=INSTRUCTIONS, **kwargs)

    # -- tools ----------------------------------------------------------------------------------------------------
    @mcp.tool()
    def get_daily_brief(date: DateArg = None, max_age_minutes: MaxAge = None) -> dict[str, Any]:
        """Today's coaching brief: flags, readiness heuristic (0-100 with reasons), last night's sleep (score,
        stages), HRV vs normal range, resting HR vs 7-day average, Body Battery, yesterday's stress, today's and
        yesterday's activities, training load (ATL/CTL/TSB/ACWR) and the next events. Call it first in every
        coaching conversation. Refreshes activities and today's wellness from Garmin when older than 60 min."""
        return service.get_daily_brief(date, max_age_minutes)

    @mcp.tool()
    def get_athlete_profile(sections: Annotated[list[Literal["profile", "zones", "history", "coach_notes"]] | None,
                                                Field(description="Default: all four.")] = None) -> dict[str, Any]:
        """Who the athlete is: profile (age, sports, constraints, weekly availability in minutes, advice language),
        thresholds (max HR, LTHR, FTP) and Garmin HR zones, the athletic history (past races, injuries, what worked)
        and the coaching rules agreed so far. Call it at the start of a conversation and respect the rules."""
        return service.get_athlete_profile(list(sections) if sections else None)

    @mcp.tool()
    def get_events() -> dict[str, Any]:
        """Upcoming events from the athlete's events.yaml: date, sport, distance, goal, priority A/B/C, days to go
        and training phase (base, build, peak, taper, race week). Use it for any planning question."""
        return service.get_events()

    @mcp.tool()
    def get_context_pack(name: Annotated[str, Field(description=(
            "daily | weekly | 7d | 28d | athlete | events | recent | instructions"))],
                         max_age_minutes: MaxAge = None) -> dict[str, Any]:
        """A ready-made Markdown summary (Italian): daily = today's brief, 7d/28d/weekly = day-by-day tables, weekly
        totals by sport, HR-zone distribution and efficiency trends, athlete = profile + history + rules, recent =
        last 10 activities, instructions = the coaching preamble."""
        return service.get_context_pack(name, max_age_minutes)

    @mcp.tool()
    def get_range_summary(start: StartArg = None, end: EndArg = None,
                          granularity: Literal["day", "week"] = "day",
                          max_age_minutes: MaxAge = None) -> dict[str, Any]:
        """Day-by-day (max 92 days, default last 7) or week-by-week (max 731 days, default last 28) history: load,
        ATL/CTL/TSB, readiness, sleep hours and score, HRV ms, resting HR, Body Battery at wake, stress, steps and the
        activities of each day (weekly: totals per sport). Use it for trends and reviews."""
        return service.get_range_summary(start, end, granularity, max_age_minutes)

    @mcp.tool()
    def get_training_load(start: StartArg = None, end: EndArg = None, max_age_minutes: MaxAge = None) -> dict[str, Any]:
        """Daily training-load series (max 92 days, default last 42): load (Garmin EPOC load, TRIMP fallback), ATL
        (7-day), CTL (42-day), TSB = form going into the day, ACWR (flag >1.5 or <0.8), monotony, strain, readiness.
        Local approximations, not Garmin's Training Status."""
        return service.get_training_load(start, end, max_age_minutes)

    @mcp.tool()
    def list_activities(start: StartArg = None, end: EndArg = None,
                        type: Annotated[str | None, Field(description=(
                            "Sport (run, bike, swim, strength, multisport, other) or a Garmin type key such as "
                            "trail_running"))] = None,
                        limit: Annotated[int, Field(ge=1, le=100)] = 20,
                        max_age_minutes: MaxAge = None) -> dict[str, Any]:
        """Activities newest first: id, date/time, type, duration, distance, pace or speed, HR avg/max, power,
        elevation gain, training load and training effect. Use the id with get_activity for details."""
        return service.list_activities(start, end, type, limit, max_age_minutes)

    @mcp.tool()
    def get_activity(activity_id: int, include_splits: bool = False, max_age_minutes: MaxAge = None) -> dict[str, Any]:
        """One activity in full: all summary fields (metres, seconds, m/s, bpm, watts), HR time in zones, power
        bests, TSS/IF and stamina from Garmin's detail, the structured workout followed (if any) and, with
        include_splits, Garmin's split summaries (intervals, run/walk). Fetches the detail from Garmin the first
        time for activities older than 28 days (2 calls)."""
        return service.get_activity(activity_id, include_splits, max_age_minutes)

    @mcp.tool()
    def get_sleep(date: DateArg = None, max_age_minutes: MaxAge = None) -> dict[str, Any]:
        """Sleep of the night ending on `date`: durations in seconds (total, deep, light, REM, awake), score and
        quality, sleep need in minutes, avg HR, SpO2, respiration, stress, Garmin's feedback."""
        return service.get_sleep(date, max_age_minutes)

    @mcp.tool()
    def get_hrv(date: DateArg = None, max_age_minutes: MaxAge = None) -> dict[str, Any]:
        """Overnight HRV in ms: last-night average, 5-min high, weekly average, Garmin's baseline bounds
        (balanced_low..balanced_upper = normal range), status, and the last 7 nights."""
        return service.get_hrv(date, max_age_minutes)

    @mcp.tool()
    def get_body_battery(date: DateArg = None, include_curve: bool = False,
                         max_age_minutes: MaxAge = None) -> dict[str, Any]:
        """Body Battery (0-100) for a day: at wake, high, low, charged, drained; with include_curve the intraday
        levels (local time, at most one point per 15 min)."""
        return service.get_body_battery(date, include_curve, max_age_minutes)

    @mcp.tool()
    def get_heart_rate(date: DateArg = None, include_samples: bool = False,
                       max_age_minutes: MaxAge = None) -> dict[str, Any]:
        """Resting HR, its 7-day average and the daily minimum (bpm); with include_samples the intraday HR averaged
        per 10 minutes (only available for today and yesterday)."""
        return service.get_heart_rate(date, include_samples, max_age_minutes)

    @mcp.tool()
    def get_weight(start: StartArg = None, end: EndArg = None, max_age_minutes: MaxAge = None) -> dict[str, Any]:
        """Weigh-ins (kg, body fat %, muscle mass kg) in a range, default last 90 days; falls back to the weight the
        athlete wrote in athlete.yaml when Garmin has none."""
        return service.get_weight(start, end, max_age_minutes)

    @mcp.tool()
    def get_hr_zones() -> dict[str, Any]:
        """Garmin heart-rate zones per sport (zone 1-5 floors in bpm, max HR, resting HR, LTHR) plus the athlete's
        own max HR / LTHR / FTP overrides."""
        return service.get_hr_zones()

    @mcp.tool()
    def append_coach_note(text: Annotated[str, Field(description="One sentence, max 500 characters.")]) -> dict[str, Any]:
        """Record a standing coaching rule or agreement (e.g. "long run on Saturdays") as a dated line in the
        athlete's coach-notes.md, so it applies in every future conversation. Only for durable agreements the user
        confirmed, not for daily data or short-term plans (use update_coaching_state). Writes only to
        coach-notes.md; it never touches Garmin."""
        return service.append_coach_note(text)

    @mcp.tool()
    def get_coaching_state() -> dict[str, Any]:
        """The athlete's short-term coaching state from earlier chats: symptoms they reported, the plan agreed for the
        next days with the conditions that change it, decisions open and taken; with its age, `stale` (older than
        `stale_after_days`: confirm it with the athlete before using it), `plan_expired` and `decisions_due`. The
        daily brief already embeds the same object: call this to re-read it after an update. Local file only."""
        return service.get_coaching_state()

    @mcp.tool()
    def update_coaching_state(
            symptoms: Annotated[list[SymptomIn] | None, Field(description=(
                "All current reported symptoms (max 8); [] clears them."))] = None,
            plan: Annotated[PlanIn | None, Field(description="The whole agreed plan; {} clears it.")] = None,
            decisions: Annotated[DecisionsIn | None, Field(description=(
                "All open and taken decisions; {} clears them."))] = None,
            confirm: Annotated[bool, Field(description=(
                "true re-dates the stored state unchanged, after the athlete confirmed it."))] = False,
    ) -> dict[str, Any]:
        """Overwrite the athlete's short-term coaching state: symptoms they reported, the plan agreed for the next
        days with the conditions that change it, decisions open and taken. Pass only the sections that changed, each
        in full (an omitted section is kept, an empty one cleared); confirm=true re-dates an unchanged state. Never
        put Garmin values, durable rules (use append_coach_note) or past sessions here. Local file only, never
        touches Garmin."""
        return service.update_coaching_state(
            None if symptoms is None else [s.model_dump(exclude_none=True) for s in symptoms],
            None if plan is None else plan.model_dump(by_alias=True, exclude_none=True),
            None if decisions is None else decisions.model_dump(exclude_none=True), confirm)

    @mcp.tool()
    def get_sync_status() -> dict[str, Any]:
        """State of the local Garmin cache: whether history is loaded, age of each data tier vs its TTL,
        the running sync job with progress (calls_done/calls_planned) and ETA, recent jobs, cooldowns and last
        error. Use it to answer "is my data up to date / loaded?". Never contacts Garmin."""
        return service.get_sync_status()

    @mcp.tool()
    def refresh(tiers: Annotated[list[Literal["A", "B", "C", "D", "E"]] | None, Field(description=(
                    "A activities, B today's wellness, C last 7 days, D optional single-day data, E profile/zones. "
                    "Default all."))] = None,
                force: Annotated[bool, Field(description="Refetch even if within the TTL.")] = False,
                scope: Annotated[Literal["tiers", "history"], Field(description=(
                    "history = (re)load the whole history in the background"))] = "tiers") -> dict[str, Any]:
        """Start a background reload from Garmin and return its job_id immediately ("I just synced my watch":
        tiers A,B with force=true). Follow progress with get_sync_status. At most once per tier every 5 minutes;
        refused while Garmin is in cooldown (rate-limit protection)."""
        return service.refresh(list(tiers) if tiers else None, force, scope)

    @mcp.tool()
    def cancel_job(job_id: int) -> dict[str, Any]:
        """Cancel a queued or running sync job (see get_sync_status). Data already fetched is kept."""
        return service.cancel_job(job_id)

    # -- resources ------------------------------------------------------------------------------------------------
    def _pack_resource(alias: str) -> Any:
        def read() -> str:
            texts, _ = service.pack_texts((alias,))
            return "\n\n".join(texts.values())

        return read

    for name, alias in PACK_RESOURCES.items():
        mcp.resource(f"garmin-coach://context/{name}", name=name, mime_type="text/markdown",
                     description=f"Context pack '{name}' (Markdown, Italian), refreshed on read when stale.")(
            _pack_resource(alias))

    @mcp.resource("garmin-coach://knowledge/history", name="history", mime_type="text/markdown",
                  description="The athlete's narrative history (history.md).")
    def knowledge_history() -> str:
        return service.knowledge_text("history")

    @mcp.resource("garmin-coach://knowledge/coach-notes", name="coach-notes", mime_type="text/markdown",
                  description="Coaching rules agreed so far (coach-notes.md).")
    def knowledge_notes() -> str:
        return service.knowledge_text("coach-notes")

    @mcp.resource("garmin-coach://knowledge/coaching-state", name="coaching-state", mime_type="application/yaml",
                  description="The current short-term coaching state (coaching-state.yaml).")
    def knowledge_state() -> str:
        return service.knowledge_text("coaching-state")

    @mcp.resource("garmin-coach://status", name="status", mime_type="application/json",
                  description="Sync status: history loaded, cache age per tier, running job progress.")
    def status_resource() -> str:
        return json.dumps(service.get_sync_status(), default=str)

    # -- prompts --------------------------------------------------------------------------------------------------
    @mcp.prompt(description="Daily check-in: today's brief + profile + events -> today's session.")
    def daily_checkin() -> str:
        return prompts.daily_checkin()

    @mcp.prompt(description="Weekly review: last 7 and 28 days -> what went well, what to change next week.")
    def weekly_review() -> str:
        return prompts.weekly_review()

    @mcp.prompt(description="Plan the next 7 days from events, load, recovery and availability.")
    def plan_week() -> str:
        return prompts.plan_week()

    @mcp.prompt(description="Readiness for an event: phase, taper, expected form. Default: the next A-priority event.")
    def race_readiness(event: str = "") -> str:
        return prompts.race_readiness(event)

    @mcp.prompt(description="Explain a metric (e.g. TSB, ACWR, readiness, HRV) and the athlete's current value.")
    def explain_metric(name: str) -> str:
        return prompts.explain_metric(name)

    return mcp


def serve(app: App, *, http: bool = False, port: int = 8765) -> None:
    """stdio by default; ``http`` = streamable HTTP on 127.0.0.1 only, with a random bearer token (PLAN §9)."""
    try:
        job = startup_check(app.jobs, app.settings, trigger="startup")
        if job:
            log.info("mcp: startup check enqueued job #%d", job)
    except Exception:  # noqa: BLE001 - never crash at boot
        log.exception("startup check failed")
    if not http:
        build_server(app).run(transport="stdio")
        return
    token = secrets.token_urlsafe(32)
    server = build_server(app, http_token=token, port=port)
    # The token protects a local session only; it is not a Garmin credential and goes to the terminal, not the log.
    print(f"garmin-coach MCP: http://127.0.0.1:{port}/mcp\nAuthorization header value: Bearer {token}",
          file=sys.stderr, flush=True)
    server.run(transport="streamable-http", host="127.0.0.1", port=port)
