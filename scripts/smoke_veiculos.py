"""Conversa de fumaça do tenant zoi_veiculos, com LLM de verdade.

    uv run python scripts/smoke_veiculos.py
    uv run python scripts/smoke_veiculos.py "ordem natural"

Não é teste: é o menor roteiro que percorre o caminho caro do fluxo
(abertura → busca → apresentação → troca → financiamento) e imprime o que
cada turno produziu. Serve para ver o agente falar antes de gastar um bot
do Telegram nele.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "src"))

from zoi_agno.pipeline import Pipeline
from zoi_agno.state import new_session_state
from zoi_agno.tenants import load_tenant

ROTEIROS: dict[str, list[str]] = {
    # Caminho feliz: o lead responde na ordem do roteiro e a conversa fecha.
    "ordem natural": [
        "oi",
        "era um SUV, até uns 120 mil",
        "Marcos",
        "vocês abrem domingo?",
        "com quais bancos vocês trabalham?",
        "gostei do Duster, quero esse",
        "tenho sim, um Onix 2018, 60 mil km, quitado",
        "seria financiado",
        "tenho entrada sim. CPF 529.982.247-25, nasci em 10/05/1990",
    ],
    # Reprodução da sessão de 2026-08-25, que falhou de três jeitos: saudação
    # repetida, nome inventado ("Marcos", vindo de um few-shot) e volta à
    # apresentação depois da escolha já confirmada. O lead responde fora de
    # ordem de propósito — é assim que gente real escreve.
    "fora de ordem": [
        "oi",
        "Olá, estou vendo a Tracker, ainda disponível?",
        "Pode ser essa mesmo!",
        "Meu nome é Raul",
        "Tenho uma Ecosport 2012\nAceitam?",
        "2012, uns 90 mil km, quitado",
        "seria financiado",
        "tenho entrada sim. CPF 529.982.247-25, nasci em 10/05/1990",
    ],
}


async def main(argv: list[str]) -> int:
    if not os.getenv("OPENAI_API_KEY"):
        for linha in (RAIZ / ".env").read_text(encoding="utf-8").splitlines():
            if "=" in linha and not linha.strip().startswith("#"):
                k, _, v = linha.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

    t = load_tenant("zoi_veiculos", base_dir=RAIZ / "tenants")
    p = Pipeline(t)
    escolhidos = argv or list(ROTEIROS)

    for nome in escolhidos:
        falas = ROTEIROS.get(nome)
        if falas is None:
            print(f"roteiro {nome!r} não existe; use: {', '.join(ROTEIROS)}", file=sys.stderr)
            return 2
        print(f"\n\033[1m═══ roteiro: {nome} ═══\033[0m")
        st = new_session_state(
            thread_id=f"smoke:{nome}",
            tenant_id=t.tenant_id,
            contact_id="1",
            start_node=t.start_node,
        )
        for fala in falas:
            print(f"\n\033[36m> {fala}\033[0m")
            r = await p.rodar_turno(st, fala)
            print(f"\033[32m{r.texto}\033[0m")
            print(f"  [nó={r.node_id} fim={r.finished} handoff={r.handoff}]")
            if r.finished:
                break

        coletado = {
            k: v for k, v in (st.get("collected") or {}).items() if not isinstance(v, dict)
        }
        print("\n--- coletado ---")
        for k, v in coletado.items():
            print(f"  {k}: {v}")
        estoque = (st.get("collected") or {}).get("estoque") or {}
        print(f"--- nó final: {st.get('current_node')} · estoque total={estoque.get('total')} ---")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
