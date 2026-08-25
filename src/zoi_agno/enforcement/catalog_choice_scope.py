"""Item escolhido tem que existir no catálogo oferecido.

Irmã da ``AppointmentSlotScopeRule``, para a outra metade do mesmo problema:
lá é horário que tem que vir da agenda, aqui é item que tem que vir da busca.

``set_slot`` no slot de escolha só passa se o valor for um id que o payload da
busca devolveu. Rejeição **soft**: o comando cai, o resto do lote segue, e a
rejeição entra no grounding do próximo turno — o extrator vê o erro e corrige.
Sem payload no estado → no-op, porque a conversa nem chegou a apresentar nada.

**Por que existe** (medição zoi_veiculos, 2026-08-25): pedindo 5 vezes a mesma
escolha ao extrator, 2 vezes ele gravou ``veiculo_escolhido`` com o nome
legível — ``"Renault Duster Iconic 2020"`` — em vez do ``codigo`` ``SUV-005``.
O guard do roteiro (``d_escolha_valida``) testa só a PRESENÇA do campo, então
o valor inválido passava e a qualificação seguia apontando para nada. Nome
legível não casa com item nenhum na hora de fechar.

Configuração, em ``business.yaml``::

    escolha_de_item:
      slot: veiculo_escolhido      # sem default: mapa ausente => rule inerte
      payload: estoque             # onde a tool gravou o resultado
      lista: candidates            # default: candidates
      campo_id: codigo             # default: codigo

Sem o mapa, a rule é no-op — vertical que não tem catálogo não paga nada por
ela. Os nomes são configuráveis pela mesma razão da rule de agenda: proteção
que depende de o autor adivinhar o nome do campo não protege vertical nova.
"""

from __future__ import annotations

from typing import Any

from zoi_agno.enforcement.rule import Rejection

_LISTA_PADRAO = "candidates"
_CAMPO_ID_PADRAO = "codigo"


def _payload_get(payload: Any, key: str) -> Any:
    """Aceita payload dict (stub de teste) e pydantic (comando real)."""
    if isinstance(payload, dict):
        return payload.get(key)
    return getattr(payload, key, None)


class CatalogChoiceScopeRule:
    name = "catalog_choice_scope"

    async def check(
        self,
        cmd: Any,
        state: dict[str, Any],
        ctx: dict[str, Any],
    ) -> Rejection | None:
        if getattr(cmd, "kind", None) != "set_slot":
            return None

        cfg = (ctx.get("business") or {}).get("escolha_de_item") or {}
        alvo = str(cfg.get("slot") or "")
        chave_payload = str(cfg.get("payload") or "")
        if not alvo or not chave_payload:
            return None

        payload = getattr(cmd, "payload", None)
        if str(_payload_get(payload, "slot") or "") != alvo:
            return None

        catalogo = (state.get("collected") or {}).get(chave_payload) or {}
        if not isinstance(catalogo, dict):
            return None
        lista = str(cfg.get("lista") or _LISTA_PADRAO)
        campo_id = str(cfg.get("campo_id") or _CAMPO_ID_PADRAO)
        oferecidos = {
            str(item.get(campo_id))
            for item in (catalogo.get(lista) or [])
            if isinstance(item, dict) and item.get(campo_id)
        }
        # Busca ainda não rodou (ou voltou vazia): não há o que validar contra.
        if not oferecidos:
            return None

        value = str(_payload_get(payload, "value") or "")
        if value in oferecidos:
            return None
        return Rejection(
            rule=self.name,
            code="choice_not_in_catalog",
            command_kind=cmd.kind,
            detail=(
                f"{alvo} {value[:48]!r} não é um {campo_id} do que foi oferecido — "
                f"use exatamente um {campo_id} de {chave_payload}.{lista}"
            ),
            extra={"offered": sorted(oferecidos)},
            command=cmd,
        )
