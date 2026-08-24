"""Command schema — o vocabulário fechado de mutação de estado.

O LLM não muda o mundo direto: ele **pede**, emitindo comandos desta união
discriminada por ``kind``. Entre o pedido e o efeito existe a fiscalização
(``zoi_agno/enforcement/``), que aceita, descarta, reescreve ou aborta.

São 15 kinds. Se o kind não está aqui, é impossível — e é isso que torna o
comportamento do agente enumerável.

Portado do runtime v4 (``zoi_agent/agent_loop_v4/commands.py``) sem alteração
de contrato: os goldens comparam os dois runtimes, então divergir o schema
invalidaria a comparação.

Validation split (FIND-025)
---------------------------

Payload classes in this module perform **single-field validation only** —
type coercion, ``Field(min_length=…)``, ``Literal[...]`` enums, etc.

**Cross-field validation lives in enforcement rules**, not in pydantic models
on the Payload. Examples:

- ``ReplanPayload.new_plan`` reachability → ``PlanReachabilityRule``.
- ``StartSubflowPayload.inputs`` required-set match → ``SubflowRule``.
- ``SetSlotPayload.slot`` ∈ current-collect-group → ``SlotScopeRule``.
- ``ConfirmSlotPayload.confidence`` low-confidence gating → ``ConfidenceRule``.

Rationale: enforcement rules already see the full ``AgentLoopState`` +
``RuntimeContextV4`` (graph topology, persona constraints, cost caps),
which pydantic ``model_validator`` cannot. Keeping pydantic single-field-only
preserves discriminator-fast parsing in the hot path and centralizes
soft/hard/never decisions in a single layer.

Procedure to add a new command kind
-----------------------------------

The spec originally listed 12 canonical command kinds. ``record_fact`` was
added as the 13th; ``annotate_interaction`` is the 14th; ``send_album`` is the
15th. If you ever need a 16th, **five** constructs must be updated in lockstep —
see ``test_command_kinds_count`` for the compile-time guard that fails when they
drift:

1. ``CommandKind`` Literal — add the string literal arm.
2. Define a payload class extending ``BaseModel`` with its required fields.
3. Define a per-kind command class extending ``_CmdBase`` with
   ``kind: Literal["<new_kind>"]`` and ``payload: <PayloadClass>``.
4. Extend the ``_AnyCommand`` Annotated tuple (the discriminated union body)
   to include the new command class.
5. Add at least one entry to ``zoi_agent/agent_loop_v4/few_shots/<new_kind>.jsonl``
   (the FewShotLoader expects per-kind files) and include the kind in
   ``command_prompt_builder._CANONICAL_KINDS``.

After those code changes:

- Update spec §4 with the new kind's row.
- Re-run ``test_command_kinds_count`` and ``test_few_shots_min_count``.

The discriminator on ``kind`` keeps Pydantic-level validation fast: do **not**
swap the union for a ``model_validator(mode="before")`` polymorphism dance —
the discriminator approach is intentional for telemetry stability and
exhaustive matching downstream.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field, RootModel

from zoi_agno._schema_helpers import truncate_str

CommandKind = Literal[
    "set_slot",
    "confirm_slot",
    "skip_collect",
    "start_subflow",
    "cancel_subflow",
    "clarify",
    "replan",
    "handoff_human",
    "consult_faq",
    "say_freetalk",
    "signal",
    "finish_flow",
    "record_fact",
    "annotate_interaction",
    "send_album",
]


# === Payload classes ===


class SetSlotPayload(BaseModel):
    slot: str = Field(min_length=1)
    value: Any


class ConfirmSlotPayload(BaseModel):
    slot: str = Field(min_length=1)
    proposed_value: Any


class SkipCollectPayload(BaseModel):
    node_id: str = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=200)


class StartSubflowPayload(BaseModel):
    ref: str = Field(min_length=1)
    inputs: dict[str, Any] = Field(default_factory=dict)


class CancelSubflowPayload(BaseModel):
    reason: str = Field(min_length=1, max_length=200)


class ClarifyPayload(BaseModel):
    question: str = Field(min_length=1, max_length=400)
    options: list[str] = Field(default_factory=list, max_length=8)


class _ReplanStep(BaseModel):
    step_id: str
    intent: Literal["collect_slot", "ask_group", "decide", "say", "subflow", "freetalk", "end"]
    target: str
    rationale: Annotated[str, BeforeValidator(truncate_str(150))] = Field(max_length=150)
    status: Literal["pending", "in_progress", "done", "skipped"] = "pending"
    estimated_turns: int = 1


class ReplanPayload(BaseModel):
    new_plan: list[_ReplanStep] = Field(min_length=1, max_length=12)
    reason: str = Field(min_length=1, max_length=200)


class HandoffHumanPayload(BaseModel):
    reason: str = Field(min_length=1, max_length=200)
    urgency: Literal["low", "med", "high"] = "med"


class ConsultFAQPayload(BaseModel):
    query: str = Field(min_length=1, max_length=400)


class SayFreetalkPayload(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class SignalPayload(BaseModel):
    name: str = Field(min_length=1)
    value: Any


class FinishFlowPayload(BaseModel):
    outcome: Literal["completed", "abandoned", "handed_off"]


class RecordFactPayload(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    value: Any


class AnnotateInteractionPayload(BaseModel):
    subtype: str = Field(min_length=1, max_length=40)
    detail: str = Field(default="", max_length=200)


class SendAlbumPayload(BaseModel):
    item_id: str = Field(min_length=1)


# === Per-kind Command wrappers (for discriminated union) ===


class _CmdBase(BaseModel):
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    rationale: Annotated[str, BeforeValidator(truncate_str(200))] = Field(
        default="", max_length=200
    )


class SetSlotCommand(_CmdBase):
    kind: Literal["set_slot"]
    payload: SetSlotPayload


class ConfirmSlotCommand(_CmdBase):
    kind: Literal["confirm_slot"]
    payload: ConfirmSlotPayload


class SkipCollectCommand(_CmdBase):
    kind: Literal["skip_collect"]
    payload: SkipCollectPayload


class StartSubflowCommand(_CmdBase):
    kind: Literal["start_subflow"]
    payload: StartSubflowPayload


class CancelSubflowCommand(_CmdBase):
    kind: Literal["cancel_subflow"]
    payload: CancelSubflowPayload


class ClarifyCommand(_CmdBase):
    kind: Literal["clarify"]
    payload: ClarifyPayload


class ReplanCommand(_CmdBase):
    kind: Literal["replan"]
    payload: ReplanPayload


class HandoffHumanCommand(_CmdBase):
    kind: Literal["handoff_human"]
    payload: HandoffHumanPayload


class ConsultFAQCommand(_CmdBase):
    kind: Literal["consult_faq"]
    payload: ConsultFAQPayload


class SayFreetalkCommand(_CmdBase):
    kind: Literal["say_freetalk"]
    payload: SayFreetalkPayload


class SignalCommand(_CmdBase):
    kind: Literal["signal"]
    payload: SignalPayload


class FinishFlowCommand(_CmdBase):
    kind: Literal["finish_flow"]
    payload: FinishFlowPayload


class RecordFactCommand(_CmdBase):
    kind: Literal["record_fact"]
    payload: RecordFactPayload


class AnnotateInteractionCommand(_CmdBase):
    kind: Literal["annotate_interaction"]
    payload: AnnotateInteractionPayload


class SendAlbumCommand(_CmdBase):
    kind: Literal["send_album"]
    payload: SendAlbumPayload


_AnyCommand = Annotated[
    SetSlotCommand
    | ConfirmSlotCommand
    | SkipCollectCommand
    | StartSubflowCommand
    | CancelSubflowCommand
    | ClarifyCommand
    | ReplanCommand
    | HandoffHumanCommand
    | ConsultFAQCommand
    | SayFreetalkCommand
    | SignalCommand
    | FinishFlowCommand
    | RecordFactCommand
    | AnnotateInteractionCommand
    | SendAlbumCommand,
    Field(discriminator="kind"),
]


class Command(RootModel[_AnyCommand]):
    """Discriminated by `kind` — Pydantic selects the correct payload class."""

    @property
    def kind(self) -> CommandKind:
        return self.root.kind

    @property
    def payload(self) -> Any:
        return self.root.payload

    @property
    def confidence(self) -> float:
        return self.root.confidence

    @property
    def rationale(self) -> str:
        return self.root.rationale

    def model_dump_compat(self) -> dict[str, Any]:
        """Compatibility shape used by audit + command_log channels."""
        return {
            "kind": self.kind,
            "payload": self.payload.model_dump(),
            "confidence": self.confidence,
            "rationale": self.rationale,
        }


class CommandGenOutput(BaseModel):
    # P7 fix 2026-05-26 — raised 6 → 12 to support principle B multi-extract.
    # Imob_sdr qualif CollectGroup has 10 fields; lead front-load can populate
    # all 10 + 2 canonical inferred (cidade from regiao, etc.) in 1 turn.
    # Previous cap rejected entire output via pydantic validation → fallback
    # to clarify ("Desculpa, pode repetir?") — broke principle B on T01.
    commands: list[Command] = Field(default_factory=list, max_length=12)
    state_summary: str = Field(default="", max_length=500)
    focus_item_id: str | None = Field(
        default=None,
        description="codigo do item que ESTE turno discutiu/focou (validado; inventado é ignorado)",
    )
    objection: str | None = Field(
        default=None,
        description="tipo de objeção detectada ESTE turno (só os ids listados no prompt; null se nenhuma)",
    )
    # Task 6 (recomendacao consultiva) — on-demand recommendation-request
    # detection, mirrors `objection` above. Only meaningful when the tenant
    # opted in (business.yaml::recommendation.enabled=True); turn_manager
    # stamps state["_recommendation_request_this_turn"] from this value
    # gated on that config (Task 7 consumes the stamp in the reasoner).
    recommendation_request: bool = Field(
        default=False,
        description=(
            "true se o lead pediu uma indicação/recomendação direta ESTE turno "
            "(ex.: 'qual você indica?'); false caso contrário"
        ),
    )
