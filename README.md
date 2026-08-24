# zoi-agno

Runtime de agentes conversacionais da ZOI sobre [Agno](https://docs.agno.com/).

Reimplementação do modelo de composição do runtime v4 (`zoi-agent`) — roteiro YAML, comandos tipados, fiscalização, executor determinístico e grounding — trocando a orquestração LangGraph pelo Workflow do Agno.

> **Estado:** Fase 2 de 10. Esqueleto de pé, contrato com o Agno validado por teste, e a fiscalização portada com as 20 rules na ordem. O executor e os cérebros ainda não existem.

## A ideia central

> **O LLM interpreta e redige. O código decide e executa.**

O fluxo da conversa vive num `routine.yaml` versionado, não num prompt gigante. O LLM lê a fala do lead e emite **comandos** de uma lista fechada de 15; a fiscalização decide quais viram realidade; um executor determinístico move o cursor do grafo; e só então um redator escreve a resposta, limitado ao que os dados sustentam.

O LLM nunca decide "agora vou pular pra qualificação". Ele diz "o lead escolheu a M16". Quem move o roteiro é código Python.

## Arquitetura

```
Workflow(session_state=slots, db=PostgresDb(...))
  ├─ Step  "ingress"    função — redige PII, carrega fatos
  ├─ Step  "planner"    Agent  🤖
  ├─ Step  "extract"    Agent  🤖  output_schema=CommandGenOutput
  ├─ Step  "enforce"    função — ~20 rules: soft / hard / never / transform
  ├─ Step  "apply"      função — comandos aceitos viram estado
  ├─ Router "advance"   função — O EXECUTOR: 8 tipos de nó, zero LLM
  ├─ Step  "compose"    Agent  🤖  persona + grounding
  ├─ Step  "critic"     Agent  🤖  com portão (decisão irreversível)
  ├─ Step  "tone"       Agent  🤖  registro de WhatsApp
  ├─ Step  "guards"     função — anti-invenção, frases proibidas
  └─ Step  "finalize"   função — custo, transcript, memória
```

**Regra de fronteira:** todo Step `executor=` é função pura sobre `session_state` e nunca chama LLM. Todo LLM está num Step `agent=`. Isso torna o pipeline testável sem rede.

## O contrato com o Agno 3.0

A premissa que sustenta o executor determinístico, descoberta por experimento e travada em `tests/test_agno_contract.py`:

```python
def meu_step(step_input: StepInput, run_context) -> StepOutput:
    estado = run_context.session_state  # o dicionário VIVO
    estado["current_node"] = "d_proximo"  # mutação in-place propaga
```

Um step-função que declara `run_context` recebe o `session_state` vivo; a escrita é vista pelos steps seguintes e persistida no `db` por `session_id`. Isso substitui o checkpointer do LangGraph.

⚠️ **A injeção é por inspeção de assinatura.** Uma função que esquece de declarar `run_context` roda normalmente e não vê estado nenhum — falha silenciosa, não exceção. A documentação do Agno mostra `step_input.session_state`, que é da linha 2.x e devolve `None` aqui.

## Estrutura

| Módulo | Responsabilidade |
|---|---|
| `contracts.py` | Os 15 comandos (união discriminada por `kind`) |
| `state.py` | O `session_state` de uma conversa |
| `tenants.py` | Carrega os artefatos YAML de um tenant |
| `enforcement/` | As 20 rules, na ordem, + o barramento soft/hard/never/transform |
| `executor/` | *(Fase 3)* o `advance()` — 8 tipos de nó |
| `brains/` | *(Fase 4)* planner, extrator, redator, crítico, tom |
| `builder.py` | *(Fase 4)* `RoutineAst → agno.Workflow` |

**Regra dura:** nada em `zoi_agno/` conhece o nome de um tenant. Vertical nova é pasta nova em `tenants/`, zero linha de Python.

## A fiscalização

Todo comando emitido pelo LLM passa por uma fila de 20 rules, **nesta ordem**. Cada uma pode deixar passar, descartar (*soft*), abortar o lote (*hard*), reclamar mas aceitar (*never* — só `handoff_human` e `finish_flow`), ou **reescrever** o comando.

Um exemplo de cada categoria e o bug que ela previne:

| Rule | Bug real que ela mata |
|---|---|
| `slot_scope` | IA anota "urgência" enquanto ainda pergunta o nome |
| `branch_gating_slot` | IA marca `tem_modelo=sim` na abertura e desvia o fluxo inteiro |
| `interrogative_user_msg` | Lead pergunta "tem outro?" e vira um `sim` extraído |
| `slot_validator` | `forma_pagamento = "pinguim"` |
| `appointment_slot_scope` | Agenda um horário que não está na agenda |
| `signal_normalize` | `pedi_corretor` nunca casa com `pediu_corretor`, e o turno entra em loop |
| `signal_guard` | `recusou` disparado por qualquer frase que contenha "não" |
| `confidence` | Dado incerto vira verdade em vez de virar confirmação |
| `finish_flow_graph` | IA encerra a conversa logo após a última coleta |
| `album_scope` | Promete foto de um item que não existe, ou que não tem foto |

**A ordem é semântica, não estilo.** `interrogative_user_msg` roda antes de `slot_validator` porque, invertido, o `sim` alucinado passa pela checagem de forma. `tests/test_enforcement_order.py` congela a sequência.

**O escape universal:** `handoff_human` sobrevive a um `hard break` no meio do lote. Se o lead pediu um humano, nenhuma falha anterior engole o pedido.

## Um tenant

Sete arquivos YAML, cada um com uma responsabilidade:

| Arquivo | Papel |
|---|---|
| `<nome>.routine.yaml` | o grafo — slots, nós, branches, sub-rotinas |
| `persona.yaml` | identidade, voz, frases proibidas, few-shots |
| `business.yaml` | regras que viram código, não prosa de prompt |
| `config.yaml` | tools de domínio (catálogo, agenda) |
| `routing.yaml` | modelo por papel + cadeia de fallback |
| `goldens.yaml` | conversas do gate de CI |

O formato da routine vive em [`zoi-routine`](https://github.com/raul-behnke/zoi-routine), compartilhado com o runtime v4 — é o que torna possível rodar um tenant de produção nos dois runtimes e comparar.

## Desenvolvimento

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
```

Para carregar tenants de outro diretório:

```bash
ZOI_TENANTS_DIR=/caminho/para/tenants uv run pytest
```
