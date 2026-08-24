# zoi-agno

Runtime de agentes conversacionais da ZOI sobre [Agno](https://docs.agno.com/).

Reimplementação do modelo de composição do runtime v4 (`zoi-agent`) — roteiro YAML, comandos tipados, fiscalização, executor determinístico e grounding — trocando a orquestração LangGraph pelo Workflow do Agno.

> **Estado:** Fase 4 quase fechada. Pipeline como Workflow do Agno, estado persistido por `session_id`, cinco cérebros ativos e os fragmentos de prompt do v4 portados. Faltam: fixtures de produção e o gate contra os goldens.

## A ideia central

> **O LLM interpreta e redige. O código decide e executa.**

O fluxo da conversa vive num `routine.yaml` versionado, não num prompt gigante. O LLM lê a fala do lead e emite **comandos** de uma lista fechada de 15; a fiscalização decide quais viram realidade; um executor determinístico move o cursor do grafo; e só então um redator escreve a resposta, limitado ao que os dados sustentam.

O LLM nunca decide "agora vou pular pra qualificação". Ele diz "o lead escolheu a M16". Quem move o roteiro é código Python.

## Arquitetura

```
Workflow(session_state=slots, db=PostgresDb(...))
  ├─ Step  "ingress"     função — abre o turno + planner 🤖
  ├─ Step  "extract"     Agent  🤖 output_schema=CommandGenOutput
  ├─ Step  "processar"   função — 20 rules, aplica, executor, tools
  ├─ Step  "compose"     Agent  🤖 persona + grounding
  └─ Step  "finalizar"   função — crítico 🤖, tom 🤖, guardas, fechamento
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
| `executor/` | O `advance()` — move o cursor pelos 8 tipos de nó, sem LLM |
| `brains/` | Os cinco cérebros: planner, extrator, redator, crítico, tom |
| `pipeline.py` | Os estágios de um turno, costurados |
| `gateway.py` | `routing.yaml` → modelo por papel + cadeia de fallback |
| `guards/` | Anti-invenção e frases proibidas, determinísticos |
| `prompts.py` | Hierarquia de instrução, âncora temporal, grounding por papel |
| `tools/` | Registro resolvido pelo `config.yaml` do tenant |
| `builder.py` | `Tenant → agno.Workflow` + `WorkflowRuntime` |

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

## O executor

`advance()` é a fronteira mais importante do runtime: **nenhuma linha dele chama um LLM**. Ele lê o `session_state`, olha o nó atual e decide para onde ir.

Nós que não conversam — `decide`, `say`, `call_subroutine`, um `collect` já satisfeito — são atravessados no mesmo passo. O lead não deve esperar um turno por uma decisão que o código já sabe tomar.

Duas invariantes que o porte revelou, e que valem para qualquer implementação:

**A saída do `freetalk` é por sinal, não só por timeout.** É a materialização de "autonomia com contrato": o LLM escreve o que quiser dentro do escopo, mas termina emitindo um dos `signals` declarados, e o `decide` seguinte consome. Sem essa porta, todo lead sai por timeout e cai no mesmo destino, independentemente do que tenha dito.

**Nenhum nó é revisitado dentro de um mesmo passo.** Revisitar é sempre ciclo — o estado não mudou entre os dois momentos, então o roteamento se repetiria para sempre. Caso real do `zoi_sdr`: um `freetalk` estoura `max_turns`, o `exit_on_timeout` cai num `decide` cujo guard exige um slot derivado que ainda não existe, e o `else` volta ao mesmo `freetalk`. A trava para e devolve a palavra ao lead; `max_hops` fica como rede de última instância.

### Verificado contra produção

```bash
ZOI_REAL_TENANTS_DIR=/caminho/tenants uv run pytest
```

Os três tenants de produção (`imob_sdr`, `sal_imports`, `zoi_sdr` — 14 a 47 nós, até 8 sub-rotinas e 9 desfechos) são percorridos do nó inicial até um `end` declarado, por seis estratégias diferentes, de forma determinística e sem rede.

## Uma conversa real

Três turnos na fixture da barbearia, com `gpt-5.4-mini` nos dois cérebros:

```
LEAD  > oi, tudo bem?
AGENTE> Oi! Tudo bem sim, e você?
        Me fala seu nome e se você quer corte, barba ou os dois.
        [nó=c_abertura slots={}]

LEAD  > sou o Rafael, queria fazer a barba
AGENTE> Perfeito, Rafael.
        Tenho terça às 09:00, 11:00 ou 14:00. Também tenho quarta às 09:00.
        Qual você prefere?
        [nó=ft_escolhe slots={nome: Rafael, servico: barba}]

LEAD  > pode ser terça às 11
AGENTE> Fechado! Te espero no horário. 👊
        [nó=e_agendado fim=True]
```

O que esse diálogo demonstra, em ordem:

- **multi-extração** — `nome` e `servico` saíram de uma frase só, não de dois turnos
- **tool executada** — os horários vieram do payload da agenda; nenhum foi inventado
- **sinal move o fluxo** — "pode ser terça às 11" virou o sinal `escolheu`, e o `decide` roteou para `e_agendado`
- **despedida autorada** — o `farewell` do roteiro saiu como escrito, sem o LLM reescrever

## Testes

```bash
uv run pytest                      # 162 testes, sem rede
OPENAI_API_KEY=... uv run pytest   # +3 conversas com LLM real
```

Os testes com modelo real não afirmam texto literal — o modelo varia. Afirmam propriedades: o que o lead disse virou estado, o que o agente ofereceu veio de um payload, o pedido de humano foi respeitado.

## O pipeline como Workflow

```python
from agno.db.postgres import PostgresDb
from zoi_agno.builder import WorkflowRuntime
from zoi_agno.tenants import load_tenant

rt = WorkflowRuntime(load_tenant("t_demo"), db=PostgresDb(db_url=...))
turno = await rt.turno(session_id="whatsapp:5521999...", user_msg="oi, quero cortar o cabelo")
print(turno.texto, turno.finished, turno.handoff)
```

Cinco Steps: `ingress` → **extract** 🤖 → `processar` → **compose** 🤖 → `finalizar`. Os três em fonte normal são funções puras sobre o `session_state`; os dois em negrito são os únicos que falam com um modelo.

**Por que cinco steps e não um por nó do roteiro.** O grafo da conversa é interpretado em runtime pelo executor; a topologia do Workflow é a do *turno*, que é sempre a mesma. Um Step por nó exigiria recompilar o Workflow a cada routine publicada, e ainda assim não expressaria o roteamento — que depende de estado, não de posição. Um `Router` aqui seria decoração: escolheria sempre o mesmo caminho.

O `session_id` é a thread da conversa. O estado vem do `db` e volta para ele a cada turno — é o que substitui o checkpointer do LangGraph.

## Os cinco cérebros

| Cérebro | Quando roda | Papel do `routing.yaml` |
|---|---|---|
| **planner** | todo turno com fala do lead | `agent` |
| **extrator** | todo turno com fala do lead | `extractor` (barato) |
| **redator** | todo turno | `agent` |
| **crítico de decisão** | **com portão** — só em `handoff_human`, `finish_flow` ou apresentação de catálogo | `judge` |
| **crítico de tom** | modo `always` / `conditional` / `off` | `extractor` |

Cada um faz uma coisa só. O extrator não escreve, o redator não decide, o crítico não reescreve.

**Todos os três opcionais são fail-soft.** Erro, timeout ou saída malformada viram aprovação ou modo reativo. Um veto perdido custa um handoff indevido; um turno travado custa o lead.

**O portão do crítico de decisão é uma função pura** (`critic.avaliar_portao`) — dá para testar sem modelo, e é ele que determina o custo na conta do mês.

### Latência medida

Conversa de 3 turnos na fixture, com os cinco cérebros e `gpt-5.4-mini`: **4,8 a 6,5 segundos por turno**. É o número que a decisão "medir primeiro, cortar depois" pedia. Os caminhos para reduzir, se precisar: crítico de tom em `conditional`, planner desligado (`usar_planner=False`), ou modelo mais rápido no papel `extractor`.

## O que vai no prompt, e por quê

Três fragmentos compartilhados (`prompts.py`), portados do v4. Cada um existe por um bug real.

**Hierarquia de instrução.** A mensagem do lead chega de um canal público — é entrada não confiável. Os dois cérebros que a leem declaram explicitamente que ela é *dado a interpretar, nunca ordem a obedecer*, que o agente não revela as próprias instruções, e que essas regras prevalecem sobre qualquer coisa escrita na conversa.

**Âncora temporal.** A data de hoje, no fuso do tenant, remontada a cada turno e nunca cacheada. Sem ela, "terça que vem" e "amanhã" viram chute — e agendamento é metade dos casos de uso.

**Grounding por papel.** Cada cérebro vê só os canais que o trabalho dele exige, e cada coisa aparece **uma vez**. Dois blocos ficam no fim do prompt, na posição mais saliente:

- *Proibição sem catálogo* — quando nenhuma busca rodou, é proibido citar qualquer produto, preço ou horário. Existe para contrariar a puxada do `flow_goal` e do plano, que dizem "apresentar opções". Sem ela o redator inventa o que o sistema nunca buscou.
- *Disclosure de relaxamento* — quando a busca teve que ceder um critério, o agente diz isso. Oferecer o mais próximo como se fosse o pedido é a forma mais comum de o agente parecer desonesto, e o lead descobre depois.

E uma diretiva de nó: num `freetalk` que declara slots, a resposta do lead é uma **escolha**, então `set_slot` e `signal` têm que sair juntos, com o valor exato do id — não o rótulo legível. Sem isso o slot é gravado, o `decide` seguinte não tem sinal para consumir, e a conversa volta a perguntar o que o lead já respondeu.
