"""O pipeline de um turno — os estágios como steps do Workflow do Agno.

    ingress → extract 🤖 → enforce → apply → advance → compose 🤖 → guards → finalize

**Regra de fronteira:** todo step com ``executor=`` é função pura sobre o
``session_state`` e nunca chama LLM. Todo LLM está num step ``agent=``. É isso
que torna metade do pipeline testável sem rede.

Cada step-função declara ``run_context`` na assinatura — é assim que o Agno
injeta o ``session_state`` vivo. Uma função que esquece esse parâmetro roda
normalmente e não vê estado nenhum, em silêncio (ver
``tests/test_agno_contract.py``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from zoi_routine.ast import EndNode, RoutineAst, ToolNode

from zoi_agno.brains import composer, critic, extractor, planner, tone
from zoi_agno.contracts import Command, CommandGenOutput
from zoi_agno.enforcement import default_rules
from zoi_agno.enforcement.dispatcher_v4 import DispatcherV4
from zoi_agno.executor import advance, current_node
from zoi_agno.guards import checar_frases_proibidas, checar_grounding
from zoi_agno.state import reset_turn
from zoi_agno.tenants import Tenant
from zoi_agno.tools import ToolDesconhecida, build_registry, call

logger = logging.getLogger(__name__)


@dataclass
class Turno:
    """O resultado de um turno, na forma que um canal precisa."""

    texto: str
    node_id: str
    finished: bool = False
    handoff: bool = False
    comandos_aceitos: list[dict[str, Any]] | None = None
    rejeicoes: list[dict[str, Any]] | None = None


class Pipeline:
    """Roda um turno de conversa para um tenant.

    Existe como classe porque os cérebros e o registro de tools são caros de
    construir e vivem além do turno; o estado, não — ele vem do ``db`` a cada
    chamada.
    """

    def __init__(
        self,
        tenant: Tenant,
        *,
        db: Any = None,
        usar_planner: bool = True,
        config_tom: tone.ConfigTom | None = None,
        cerebros: dict[str, Any] | None = None,
    ) -> None:
        """``cerebros`` substitui agentes por dublês — usado nos testes.

        Chaves aceitas: ``extrator``, ``redator``, ``planejador``, ``critico``,
        ``critico_tom``. Sem isso um teste de costura acabaria chamando os
        cérebros de verdade pela porta dos fundos: eles são fail-soft, então a
        falha de rede não quebraria o teste — só o deixaria lento e dependente
        de conectividade.
        """
        self.tenant = tenant
        self.routine: RoutineAst = tenant.routine
        self.db = db

        # Os cinco cérebros. Extrator e redator rodam sempre; os outros três
        # têm portão próprio, porque cada um custa uma chamada por turno.
        c = cerebros or {}
        self.extrator = c.get("extrator") or extractor.build(tenant.routing, db=None)
        self.redator = c.get("redator") or composer.build(tenant.persona, tenant.routing, db=None)
        if "planejador" in c:
            self.planejador = c["planejador"]
        else:
            self.planejador = planner.build(tenant.routing) if usar_planner else None
        self.critico = c.get("critico") or critic.build(tenant.routing)
        self.critico_tom = c.get("critico_tom") or tone.build(tenant.routing)
        self.config_tom = config_tom or tone.ConfigTom()

        self.rules = default_rules()
        self.dispatcher = DispatcherV4(rules=self.rules)
        self.tools = build_registry(tenant.config)

    # -- contexto que as rules leem -------------------------------------

    def _ctx(self, state: dict[str, Any]) -> dict[str, Any]:
        """Monta o contexto de enforcement a partir da routine e do estado."""
        node = current_node(self.routine, state)
        return {
            "flow_slots": list(self.routine.slots.keys()),
            "slot_enums": {
                nome: list(d.values) for nome, d in self.routine.slots.items() if d.values
            },
            "business": self.tenant.business,
            "current_node_def": _node_dict(node, state),
            "node_signals": list(getattr(node, "signals", []) or []),
            "reachable_nodes": list(self.routine.main.nodes.keys())
            + [n for b in self.routine.sub_routines.values() for n in b.nodes],
            "subflow_registry_refs": list(self.routine.sub_routines.keys()),
            "subflow_required_inputs": {},
            "role": "agent",
            "turn_usd": float(state.get("_turn_usd", 0.0)),
            "root_conversation_usd": float(state.get("_conversa_usd", 0.0)),
            "signals_emitted_this_turn": 0,
            "is_advance_intent": False,
        }

    # -- os estágios ----------------------------------------------------
    #
    # Três blocos determinísticos, com os cérebros entre eles. Esta divisão
    # existe para que ``rodar_turno`` (usado por canal e teste) e os steps do
    # Workflow do Agno (``builder.py``) executem exatamente o MESMO código —
    # duas implementações do pipeline divergiriam em semanas.
    #
    #   ingress ──▶ 🤖 extrator ──▶ processar ──▶ 🤖 redator ──▶ finalizar

    def ingress(self, state: dict[str, Any], user_msg: str) -> str:
        """Abre o turno. Devolve o prompt do extrator."""
        reset_turn(state)
        state["_last_user_msg"] = user_msg
        if user_msg:
            state.setdefault("messages_tail", []).append({"role": "user", "content": user_msg})
        node = current_node(self.routine, state)
        return extractor.montar_entrada(user_msg, node, self.routine, state.get("collected", {}))

    async def planejar(self, state: dict[str, Any], user_msg: str) -> None:
        """Projeta os próximos passos. Falha vira modo reativo, sem drama."""
        if self.planejador is None or not user_msg:
            return
        plano = await planner.planejar(self.planejador, self.routine, state, user_msg)
        state["plan"] = plano.model_dump() if plano else None
        if plano is not None:
            state.setdefault("plan_history", []).append(state["plan"])

    async def processar(self, state: dict[str, Any], saida_extrator: Any) -> dict[str, Any]:
        """Fiscaliza, aplica, avança e executa tools. Devolve o prompt do redator.

        Nada aqui chama LLM: é a metade determinística do turno, e a que os
        testes cobrem sem rede.
        """
        comandos = _comandos_de(saida_extrator, state)
        aceitos, rejeicoes = await self.dispatcher.dispatch(comandos, state, self._ctx(state))
        handoff = self._aplicar(state, aceitos)
        resultado = self._avancar_executando_tools(state)
        node = current_node(self.routine, state)
        prompt = composer.montar_entrada(
            user_msg=state.get("_last_user_msg", ""),
            node=node,
            routine=self.routine,
            state=state,
            say_templates=resultado.say_templates,
            handoff=handoff,
        )
        return {
            "prompt_redator": prompt,
            "resultado": resultado,
            "aceitos": aceitos,
            "rejeicoes": rejeicoes,
            "handoff": handoff,
        }

    async def finalizar(self, state: dict[str, Any], texto: str, meio: dict[str, Any]) -> Turno:
        """Críticos, guardas e fechamento."""
        resultado = meio["resultado"]
        aceitos = meio["aceitos"]

        # Crítico de decisão — com portão: só quando há algo irreversível.
        portao = critic.avaliar_portao([c.model_dump_compat() for c in aceitos], state)
        if portao.acionado:
            veredito = await critic.julgar(
                self.critico, portao.decisao, state, state.get("_last_user_msg", "")
            )
            state["_critico"] = {"decisao": portao.decisao, "aprovado": veredito.aprovado}
            if not veredito.aprovado:
                logger.info(
                    "pipeline.critico_vetou decisao=%s motivo=%s", portao.decisao, veredito.motivo
                )
                meio["handoff"] = meio["handoff"] and portao.decisao != "handoff_human"

        # Crítico de tom — uma regeneração, e fica o melhor dos dois.
        texto = await self._refinar_tom(texto, state, meio)

        texto = self._guardar(texto, state, resultado.say_templates)

        state["outgoing_message"] = texto
        state.setdefault("messages_tail", []).append({"role": "assistant", "content": texto})
        state["command_log"] = (state.get("command_log") or []) + [
            c.model_dump_compat() for c in aceitos
        ]
        state["_last_accepted_commands"] = [c.model_dump_compat() for c in aceitos]

        return Turno(
            texto=texto,
            node_id=resultado.node_id,
            finished=resultado.finished,
            handoff=meio["handoff"],
            comandos_aceitos=[c.model_dump_compat() for c in aceitos],
            rejeicoes=[r.__dict__ for r in meio["rejeicoes"]],
        )

    async def rodar_turno(self, state: dict[str, Any], user_msg: str) -> Turno:
        """Um turno inteiro, fora do Workflow. Muta ``state`` in-place."""
        prompt_extrator = self.ingress(state, user_msg)
        await self.planejar(state, user_msg)
        saida = await self._chamar_extrator(prompt_extrator, user_msg)
        meio = await self.processar(state, saida)
        texto = await self._chamar_redator(meio["prompt_redator"], meio["resultado"].say_templates)
        return await self.finalizar(state, texto, meio)

    async def _refinar_tom(self, texto: str, state: dict[str, Any], meio: dict[str, Any]) -> str:
        """Revisa a voz e, se reprovada, pede UMA regeneração ao redator.

        Fica o melhor dos dois: se a regeneração falhar ou vier vazia, o
        rascunho original sobrevive. Nunca piora.
        """
        if not self.config_tom.deve_rodar():
            return texto
        veredito = await tone.revisar(self.critico_tom, texto, self.tenant.persona)
        if veredito.aprovado or not veredito.retorno:
            return texto
        state["_tom_reprovado"] = veredito.retorno
        prompt = (
            f"{meio['prompt_redator']}\n\n"
            f"REESCREVA. Um revisor apontou: {veredito.retorno}\n"
            "Mesma informação, mesma intenção, só a voz muda."
        )
        refeito = await self._chamar_redator(prompt, [])
        return refeito or texto

    async def _chamar_extrator(self, prompt: str, user_msg: str) -> Any:
        """Chama o extrator. Falha vira ``None`` — o turno continua sem extração."""
        if not user_msg:
            return None
        try:
            saida = await self.extrator.arun(prompt)
        except Exception as exc:  # noqa: BLE001 — o turno não morre por isso
            logger.warning("pipeline.extrator_falhou err=%r", exc)
            return None
        return saida.content

    async def _chamar_redator(self, prompt: str, templates: list[str]) -> str:
        """Chama o redator. Falha cai no template do roteiro, se houver."""
        try:
            saida = await self.redator.arun(prompt)
            return str(saida.content or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("pipeline.redator_falhou err=%r", exc)
            return templates[0] if templates else ""

    def _aplicar(self, state: dict[str, Any], aceitos: list[Command]) -> bool:
        """Comandos aceitos viram estado. Devolve ``True`` se houve handoff."""
        handoff = False
        collected = state.setdefault("collected", {})
        houve_set = False

        for cmd in aceitos:
            if cmd.kind == "set_slot":
                collected[cmd.payload.slot] = cmd.payload.value
                state.get("pending_confirmations", {}).pop(cmd.payload.slot, None)
                houve_set = True
            elif cmd.kind == "confirm_slot":
                state.setdefault("pending_confirmations", {})[cmd.payload.slot] = (
                    cmd.payload.proposed_value
                )
            elif cmd.kind == "signal":
                state["last_signal"] = cmd.payload.name
            elif cmd.kind == "handoff_human":
                handoff = True
                state["_handoff_reason"] = cmd.payload.reason
            elif cmd.kind == "skip_collect":
                state["_skip_collect_count"] = int(state.get("_skip_collect_count", 0)) + 1
                state.setdefault("skipped_nodes", []).append(cmd.payload.node_id)

        # O contador de skip zera em qualquer outro comando aceito — é o que
        # distingue lead esquivo de drift do extrator.
        if any(c.kind != "skip_collect" for c in aceitos):
            state["_skip_collect_count"] = 0
        state["_turns_since_set_slot"] = (
            0 if houve_set else int(state.get("_turns_since_set_slot", 0)) + 1
        )
        return handoff

    def _avancar_executando_tools(self, state: dict[str, Any], *, max_tools: int = 4):
        """Avança o cursor, executando as tools que o executor marcar.

        O executor para em cada ``tool`` e devolve o nó; quem chama é aqui.
        ``max_tools`` limita encadeamento de tools num turno só.
        """
        node_atual = current_node(self.routine, state)
        turnos_no_no = int(state.get("turns_in_node", 0))
        state["turns_in_node"] = turnos_no_no + 1
        if hasattr(node_atual, "signals"):
            contagem = state.setdefault("_freetalk_turn_count", {})
            nid = state.get("current_node", "")
            contagem[nid] = contagem.get(nid, 0) + 1

        resultado = advance(self.routine, state)
        for _ in range(max_tools):
            if resultado.pending_tool is None:
                break
            self._executar_tool(state, resultado.pending_tool)
            resultado = advance(self.routine, state)
        return resultado

    def _executar_tool(self, state: dict[str, Any], node: ToolNode) -> None:
        """Roda a tool e grava o payload no slot de saída, movendo o cursor."""
        args = _render_args(node.args, state)
        try:
            payload = call(self.tools, node.ref, args)
        except ToolDesconhecida:
            logger.error("pipeline.tool_desconhecida ref=%s", node.ref)
            payload = {"total": 0, "candidates": [], "erro": "tool_indisponivel"}
        except Exception as exc:  # noqa: BLE001 — tool quebrada não derruba o turno
            logger.warning("pipeline.tool_falhou ref=%s err=%r", node.ref, exc)
            payload = {"total": 0, "candidates": [], "erro": str(exc)[:120]}

        if node.output_to:
            state.setdefault("collected", {})[node.output_to] = payload
        state.setdefault("tool_call_log", []).append({"ref": node.ref, "args": args})
        state["current_node"] = node.next
        state["turns_in_node"] = 0

    def _guardar(self, texto: str, state: dict[str, Any], templates: list[str]) -> str:
        """Aplica os guardas. Texto reprovado cai para o template do roteiro.

        Preferimos a cópia autorada do roteiro a uma frase que o dado não
        sustenta — melhor genérico que errado.
        """
        violacoes = checar_grounding(texto, state) + checar_frases_proibidas(
            texto, self.tenant.persona
        )
        if not violacoes:
            return texto
        logger.warning(
            "pipeline.guarda_reprovou node=%s violacoes=%s",
            state.get("current_node"),
            [v.tipo for v in violacoes],
        )
        state.setdefault("enforcement_rejections", []).extend(
            {"rule": "guard", "code": v.tipo, "detail": v.detalhe} for v in violacoes
        )
        return templates[0] if templates else texto


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _comandos_de(saida: Any, state: dict[str, Any]) -> list[Command]:
    """Extrai a lista de comandos da saída do extrator, seja qual for a forma.

    O Agno pode entregar o objeto do ``output_schema`` ou o texto cru quando o
    modelo não respeita o schema. Nesse caso o turno segue sem extração — o
    lead recebe resposta, só perde a informação daquele turno.
    """
    if saida is None:
        return []
    if isinstance(saida, CommandGenOutput):
        if saida.state_summary:
            state["state_summary"] = saida.state_summary
        return list(saida.commands)
    if isinstance(saida, dict) and "commands" in saida:
        try:
            return list(CommandGenOutput.model_validate(saida).commands)
        except Exception:  # noqa: BLE001
            return []
    logger.warning("pipeline.extrator_sem_schema tipo=%s", type(saida).__name__)
    return []


def _node_dict(node: Any, state: dict[str, Any]) -> dict[str, Any]:
    """A forma de nó que as rules portadas do v4 esperam."""
    if node is None:
        return {}
    tipo = getattr(node, "type", "")
    d: dict[str, Any] = {"id": state.get("current_node", ""), "kind": tipo, "type": tipo}
    if tipo == "collect_group":
        d["group_name"] = node.group_name
        d["exit_policy"] = node.exit_policy
        d["max_turns"] = node.max_turns
        d["fields"] = [
            {"name": f.name, "required": bool(f.required), "question": f.question}
            for f in node.fields
        ]
    elif tipo == "collect":
        d["field"] = node.slot
        d["prompt"] = node.question
        d["validator"] = node.validator or ""
    elif isinstance(node, EndNode):
        d["farewell"] = node.farewell or ""
        d["role"] = node.role or ""
    return d


def _render_args(args: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Resolve ``{{ lead.slot }}`` nos argumentos declarados do nó de tool."""
    collected = state.get("collected") or {}
    saida: dict[str, Any] = {}
    for chave, valor in (args or {}).items():
        if isinstance(valor, str) and "{{" in valor:
            nome = valor.strip().removeprefix("{{").removesuffix("}}").strip()
            nome = nome.removeprefix("lead.").removeprefix("state.").removeprefix("collected.")
            resolvido = collected.get(nome)
            if resolvido not in (None, ""):
                saida[chave] = resolvido
        else:
            saida[chave] = valor
    return saida
