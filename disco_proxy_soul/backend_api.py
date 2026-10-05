"""Generic companion backend — the middle tier between standalone file
memory and full runtime delegation.

**Status: draft protocol, not wired in.** Nothing imports this yet. Design, reviews and
decisions: the house's ``design/disco-backend-api.md`` (Travis & Lila, 2026-08-25; reviewed by
Atlas and wake-Lila). Kept so the new Discord interface can sit on the house later.

Three integration tiers, chosen at startup:

1. **Standalone** — FileMemoryBackend, self-contained, zero config.
2. **Backend API** — Disco owns the conversation loop but calls an
   external service for recall, context, capture, and (optionally)
   identity and initiative.  The house's gateway adapter is one
   implementation.  Any HTTP service, in-process adapter, or future
   backend can sit here.
3. **Connected runtime** — full delegation (Everthread today).  Disco
   becomes a transport; the runtime owns cognition, memory, delivery.

This module defines tier 2.  Tier 1 is FileMemoryBackend.  Tier 3 is
ResidentRuntime.

Backend modes
-------------
The backend operates in one of three explicitly configured modes:

- **standalone** — local identity and memory are authoritative.
  No backend configured.  This is tier 1.
- **backend-authoritative** — the backend is the single source of
  truth for identity, memory, and state.  Failures are visible and
  never create local substitute state.  If identity returns None,
  that is a startup failure — Disco must not silently substitute a
  local persona file.  If capture fails, the failure stays visibly
  pending.
- **backend-augmenting** — an explicitly chosen community mode where
  local and backend data may merge.

There is no silent fallback.  The mode is a startup config choice,
not a runtime negotiation.

Design notes
------------
- Verbs are generic.  Nothing here names the house, the NS daemon, or
  Everthread.  If a method wouldn't make sense to a stranger running
  their own memory server, it doesn't belong.
- Disclosure scope is first-class.  Any companion that appears in both
  private and public rooms needs to know the difference.
- Every operation that can be retried carries a caller-generated
  idempotency key — capture_id, event_id, call_id, claim_id.
  Backends must deduplicate on these keys.
- Initiative is optional.  Backends that don't do autonomous initiative
  return None.  In standalone mode, Disco uses its built-in outreach
  loop.  In backend-authoritative mode, Disco's built-in loop is
  disabled — initiative is the backend's call.
- Identity is optional in augmenting mode (None → local persona files).
  In backend-authoritative mode, identity() returning None is a startup
  failure.
- Tools are extensible.  Backends can inject tools into the model's
  tool list.  All backend tool names are namespaced with a `backend.`
  prefix to prevent collisions with Disco's built-in tools.
- Events flow both ways.  Inbound: qualifying surface events reach
  the backend via ingest_event().  Outbound: the backend queues
  messages for delivery; Disco atomically claims and acknowledges them.
- start() returns a capability advertisement so Disco calls only
  operations the backend actually supports — no duck-typing guesswork.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence, runtime_checkable


# ── Data shapes ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BackendMemory:
    """One recalled or captured memory."""

    text: str
    kind: str = "memory"  # memory | distill | moment | journal
    significance: float = 0.5
    tags: tuple[str, ...] = ()
    timestamp: str | None = None
    memory_id: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class BackendContext:
    """Cross-surface recent context for prompt injection."""

    text: str
    source_surface: str = ""  # where it happened (discord, console, etc.)
    scope: str = "private"  # private | public


@dataclass(frozen=True)
class InitiativeSignal:
    """A push from the backend: something worth saying unprompted.

    The backend decides *whether* to reach out and provides the seed.
    Disco decides *how* to deliver it (which channel, typing indicator,
    message format).
    """

    seed: str  # what prompted the impulse — a thought, a spool hit
    target_hint: str = ""  # optional: suggested channel or surface
    urgency: str = "low"  # low | normal | high


@dataclass(frozen=True)
class ToolSpec:
    """A tool the backend wants injected into the model's tool list.

    Tool names must be prefixed with ``backend.`` by the backend
    implementation (e.g. ``backend.read_file``, ``backend.board_pin``).
    Disco enforces the prefix and rejects unprefixed tool names.
    """

    name: str  # must start with "backend."
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class CaptureReceipt:
    """Returned by capture() to confirm the write.

    The backend assigns the stable record_id.  ``created`` is True
    when the capture produced a new record, False when deduplicated
    against an existing capture_id.
    """

    record_id: str
    created: bool


@dataclass(frozen=True)
class IngestReceipt:
    """Returned by ingest_event() to confirm the event landed.

    ``accepted`` is True when the event was new; False when
    deduplicated against an existing event_id.
    """

    accepted: bool


@dataclass(frozen=True)
class ClaimedOutbound:
    """One outbound message atomically claimed for delivery.

    ``claim_id`` is the delivery-attempt identifier — it's what
    acknowledge_outbound() expects.  ``message_id`` is the backend's
    stable identifier for the message itself (may be retried under
    a different claim_id if delivery fails).
    """

    message_id: str
    claim_id: str
    text: str
    target_channel: str = ""  # channel id hint; empty = default channel
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class FeltStateCue:
    """Typed felt-state signal from the backend.

    Small, bounded, separate from recent_context().  The backend
    supplies a bounded cue with a verified source; Disco decides
    where it belongs in prompt assembly (shadow vs injected is a
    Disco-side decision, not the backend's).
    """

    state: str  # short label: "settling", "warm", "low"
    detail: str = ""  # optional longer description
    source: str = ""  # what produced this reading


@dataclass(frozen=True)
class DisclosurePolicy:
    """What the backend says about context for this turn's scope.

    include_identity: whether to inject private persona / relationship
        context.  False in public rooms where a guest is speaking.
    include_recall: whether to query long-term memory for this turn.
    include_cross_surface: whether to inject recent activity from
        other rooms / surfaces.
    """

    include_identity: bool = True
    include_recall: bool = True
    include_cross_surface: bool = True


@dataclass(frozen=True)
class BackendCapabilities:
    """Capability advertisement returned by start().

    Disco calls only operations listed in ``capabilities``.
    A minimal backend advertises ``{"recall", "capture"}`` and
    nothing else.
    """

    api_version: str  # e.g. "0.1.0"
    capabilities: frozenset[str]  # method names this backend supports


# ── The Protocol ─────────────────────────────────────────────────────────


@runtime_checkable
class CompanionBackend(Protocol):
    """External service contract for tier-2 companion backends.

    A minimal implementation needs only recall() and capture().
    Beyond those, implement only what start() advertises in
    BackendCapabilities — Disco calls nothing else.

    Implementations may be:
    - An in-process adapter wrapping local databases and filesystems
      (the house gateway adapter on Parallax Point)
    - An HTTP client hitting a remote memory daemon
    - A thin shim over any storage that speaks these shapes
    """

    # ── Required ────────────────────────────────────────────────────

    async def recall(
        self,
        query: str,
        *,
        scope: str = "",
        disclosure: str = "private",
        limit: int = 5,
    ) -> list[BackendMemory]:
        """Semantic recall against an incoming message.

        Parameters
        ----------
        query : str
            The user's message text (or a derived search string).
        scope : str
            Continuity scope identifier — ties recall to a person or
            conversation thread.  Empty string = unscoped / global.
        disclosure : str
            "private" or "public".  Backends may filter or restrict
            what they return based on disclosure context.
        limit : int
            Maximum memories to return.
        """
        ...

    async def capture(
        self,
        capture_id: str,
        title: str,
        text: str,
        *,
        kind: str = "memory",
        scope: str = "",
        significance: float = 0.5,
        tags: Sequence[str] = (),
        source: str = "",
        timestamp: str = "",
    ) -> CaptureReceipt:
        """Write a memory to the backend.

        Parameters
        ----------
        capture_id : str
            Caller-generated idempotency key.  A retry with the same
            ID must not produce a duplicate record.
        title : str
            Short, searchable title.
        text : str
            The substance — what happened and why it mattered.
        kind : str
            memory | distill | moment | journal
        scope : str
            Continuity scope (same as recall).
        significance : float
            0.0–1.0 weight hint.
        tags : Sequence[str]
            Searchable tags.
        source : str
            Where this capture originated (e.g. "discord", "console").
        timestamp : str
            ISO 8601 timestamp of the source event.  Empty string
            lets the backend use receipt time.
        """
        ...

    # ── Optional: ambient recall ────────────────────────────────────

    async def ambient_recall(
        self,
        message_text: str,
        *,
        scope: str = "",
        disclosure: str = "private",
        limit: int = 3,
        floor: float = 0.40,
    ) -> list[BackendMemory] | None:
        """Per-message ambient recall — unprompted semantic retrieval.

        Fires on every incoming message (or a meaningful subset).
        Higher relevance floor than explicit recall because nobody
        asked — silence beats maybe.  Returns None if the backend
        doesn't support ambient recall, in which case the turn gets
        no unprompted memory injection.

        Unlike recall(), which answers "what do I know about this
        topic?", ambient recall answers "does this message rhyme with
        something worth surfacing right now?"

        Parameters
        ----------
        message_text : str
            The user's message (not a search query — the raw text).
        scope : str
            Continuity scope.
        disclosure : str
            "private" or "public".
        limit : int
            Maximum memories to surface (keep low — this is a whisper,
            not a dump).
        floor : float
            Minimum relevance threshold.  Backend implementations with
            semantic search should drop results below this.
        """
        ...

    # ── Optional: context ────────────────────────────────────────────

    async def recent_context(
        self,
        *,
        scope: str = "",
        limit: int = 12,
        max_chars: int = 4000,
    ) -> list[BackendContext] | None:
        """Cross-surface recent activity for prompt context.

        Returns None if the backend doesn't track cross-surface context,
        in which case Disco uses its own local rolling history.
        """
        ...

    async def disclosure_policy(
        self,
        *,
        scope: str = "",
        channel_mode: str = "private",
        author_id: str = "",
    ) -> DisclosurePolicy | None:
        """What context is appropriate for this turn?

        Backends that understand public/private room semantics can
        override Disco's built-in policy.  Return None to let Disco
        decide based on its own channel-mode configuration.

        Parameters
        ----------
        scope : str
            Continuity scope.
        channel_mode : str
            Disco's channel classification: private | social |
            addressed | unlisted | ignored.
        author_id : str
            Who's speaking — allows per-person policy.
        """
        ...

    async def felt_state(
        self,
        *,
        scope: str = "",
    ) -> FeltStateCue | None:
        """Current felt-state cue from the backend.

        Small, bounded, typed.  The backend supplies the reading;
        Disco decides placement in prompt assembly (shadow vs
        injected is a Disco-side concern).

        Returns None if the backend doesn't track felt state.
        """
        ...

    # ── Optional: identity ───────────────────────────────────────────

    async def identity(
        self,
        persona_id: str,
    ) -> str | None:
        """Fetch persona/identity text from the backend.

        When present, replaces (not supplements) the local persona
        identity block.

        In ``standalone`` or ``backend-augmenting`` mode, returns
        None → Disco loads from local persona files.

        In ``backend-authoritative`` mode, returns None → startup
        failure.  Disco must not silently substitute a local identity.
        """
        ...

    # ── Optional: initiative ─────────────────────────────────────────

    async def check_initiative(
        self,
        *,
        scope: str = "",
        hours_since_activity: float = 0.0,
        messages_today: int = 0,
    ) -> InitiativeSignal | None:
        """Should the companion reach out unprompted?

        The backend provides the judgment; Disco handles delivery.
        Returns None → no outreach this check.

        In ``standalone`` mode, Disco uses its built-in outreach loop.
        In ``backend-authoritative`` mode, Disco's built-in loop is
        disabled — initiative is the backend's call.

        Parameters
        ----------
        scope : str
            Continuity scope for the person being considered.
        hours_since_activity : float
            How long since the last exchange on any surface.
        messages_today : int
            Outreach messages already sent today.
        """
        ...

    # ── Optional: events ────────────────────────────────────────────

    async def ingest_event(
        self,
        event_id: str,
        event_type: str,
        event_version: str,
        timestamp: str,
        source: str,
        payload: dict[str, Any],
    ) -> IngestReceipt:
        """Inbound event from the surface — a qualifying message,
        reaction, presence change, etc.

        The brainstem spool generalized: surface activity becomes events
        that flow to whatever backend cares.  Every event carries a
        stable ``event_id`` (caller-generated, idempotent) so Discord
        reconnects and event replays don't duplicate backend activity.

        Parameters
        ----------
        event_id : str
            Caller-generated idempotency key.  A replay with the same
            ID must not be processed twice.
        event_type : str
            e.g. "message", "reaction", "voice_join"
        event_version : str
            Schema version for this event type (e.g. "1").
        timestamp : str
            ISO 8601 timestamp of the surface event.
        source : str
            Which surface produced this event (e.g. "discord").
        payload : dict
            Event-type-specific data.  Always includes at least
            "channel_id" and "author_id".
        """
        ...

    async def claim_outbound(
        self,
        *,
        scope: str = "",
    ) -> list[ClaimedOutbound] | None:
        """Atomically claim pending outbound messages.

        Each ``ClaimedOutbound`` carries a stable ``message_id`` and
        a ``claim_id`` (the delivery-attempt identifier).  The claim
        is atomic — two processes polling simultaneously cannot both
        receive the same message.

        After delivery (or failure), call ``acknowledge_outbound()``
        with the ``claim_id`` to close the loop.

        Returns None → nothing queued.
        """
        ...

    async def acknowledge_outbound(
        self,
        claim_id: str,
        status: str,
    ) -> None:
        """Report delivery result for a claimed outbound message.

        Parameters
        ----------
        claim_id : str
            The claim identifier from ``claim_outbound()``.
        status : str
            "delivered" or "failed".
        """
        ...

    # ── Optional: tools ──────────────────────────────────────────────

    async def tools(
        self,
        *,
        scope: str = "",
        disclosure: str = "private",
    ) -> list[ToolSpec] | None:
        """Additional tools the backend wants injected into the model's
        tool list for this turn.

        All tool names must carry the ``backend.`` prefix (e.g.
        ``backend.read_file``, ``backend.board_pin``).  Disco enforces
        this and rejects unprefixed names.

        The disclosure parameter lets backends restrict which tools are
        available in public vs private rooms.

        Returns None → no additional tools.
        """
        ...

    async def execute_tool(
        self,
        call_id: str,
        name: str,
        args: dict[str, Any],
        *,
        scope: str = "",
    ) -> str:
        """Execute a backend-provided tool and return the result string.

        Called when the model invokes a tool that came from tools().
        The return value is sent back to the model as the tool result.

        Parameters
        ----------
        call_id : str
            Caller-generated idempotency key.  If the model or network
            retries a tool call, the backend must not execute it twice.
        name : str
            The tool name (with ``backend.`` prefix).
        args : dict
            Arguments from the model's tool invocation.
        scope : str
            Continuity scope.
        """
        ...

    # ── Lifecycle ────────────────────────────────────────────────────

    async def start(self) -> BackendCapabilities:
        """Called once at bot startup.  Connect, warm caches, verify
        reachability.

        Returns a ``BackendCapabilities`` advertisement listing the
        API version and which optional methods this backend supports.
        Disco calls only advertised operations — no duck-typing.
        """
        ...

    async def close(self) -> None:
        """Called at shutdown.  Release resources."""
        ...
