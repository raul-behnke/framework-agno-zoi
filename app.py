"""Sobe um agente no Telegram.

    uv run python app.py t_demo

Precisa de ``OPENAI_API_KEY`` e ``TELEGRAM_BOT_TOKEN`` no ambiente (ou num
``.env`` ao lado deste arquivo, que fica fora do git).

Um processo por bot. Vertical nova é pasta nova em ``tenants/`` — nada aqui
conhece o nome de um tenant.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

# ATENÇÃO À ORDEM: o logging é configurado aqui, ANTES de importar agno e
# litellm. Essas bibliotecas mexem na configuração global no momento do
# import — chegar depois delas é uma corrida que a gente perde, e o sintoma é
# um serviço que sobe e não loga nada.
logging.basicConfig(
    level=os.getenv("ZOI_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stderr,
    force=True,
)
for ruidoso in ("LiteLLM", "litellm", "httpx", "httpcore", "openai"):
    logging.getLogger(ruidoso).setLevel(logging.WARNING)

from agno.db.sqlite import SqliteDb

from zoi_agno.builder import WorkflowRuntime
from zoi_agno.channel import BotTelegram, ConfigTelegram
from zoi_agno.pipeline import Pipeline
from zoi_agno.tenants import TenantNotFoundError, list_tenants, load_tenant
from zoi_agno.wait import RepoSQLite, WaitWorker

logger = logging.getLogger("zoi_agno.app")


def carregar_env(caminho: Path) -> None:
    """Lê um ``.env`` simples. Sem dependência: são cinco linhas."""
    if not caminho.is_file():
        return
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, _, valor = linha.partition("=")
        os.environ.setdefault(chave.strip(), valor.strip())


async def rodar(tenant_id: str, *, dados: Path, tenants_dir: Path) -> int:
    # Primeira linha do log: prova de vida antes de qualquer trabalho pesado.
    # Sem ela, "subiu e está lento" e "subiu e travou" são indistinguíveis.
    logger.info("subindo tenant=%s", tenant_id)
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print("falta TELEGRAM_BOT_TOKEN no ambiente", file=sys.stderr)
        return 2
    if not os.getenv("OPENAI_API_KEY"):
        print("falta OPENAI_API_KEY no ambiente", file=sys.stderr)
        return 2

    try:
        tenant = load_tenant(tenant_id, base_dir=tenants_dir)
    except TenantNotFoundError as exc:
        print(
            f"{exc}\ntenants disponíveis: {', '.join(list_tenants(base_dir=tenants_dir))}",
            file=sys.stderr,
        )
        return 2
    if tenant.warnings:
        for w in tenant.warnings:
            logger.warning("routine: %s", w)

    dados.mkdir(parents=True, exist_ok=True)
    db = SqliteDb(db_file=str(dados / f"{tenant_id}.db"))
    repo = RepoSQLite(dados / f"{tenant_id}-esperas.db")

    runtime = WorkflowRuntime(tenant, db=db, pipeline=Pipeline(tenant, db=db, repo_de_esperas=repo))
    bot = BotTelegram(ConfigTelegram(token=token, tenant_id=tenant_id), runtime)
    worker = WaitWorker(repo, {tenant_id: runtime})

    eu = await bot.quem_sou()
    # Tudo por logging, inclusive o banner: num container, `print` (stdout) e
    # logging (stderr) se separam, e depurar "por que não aparece nada no log"
    # custou mais que escrever isto certo.
    logger.info(
        "no ar: @%s atendendo como %s · routine %s · %d nós",
        eu.get("username"),
        tenant.persona.get("name", tenant_id),
        tenant.routine.routine_name,
        len(tenant.routine.main.nodes),
    )

    parar = asyncio.Event()
    laco = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        laco.add_signal_handler(sig, parar.set)

    async def acordar_esperas() -> None:
        """Varre as conversas estacionadas enquanto o bot atende."""
        while not parar.is_set():
            try:
                for r in await worker.tick():
                    logger.info("retomada session=%s ok=%s", r.session_id, r.ok)
            except Exception:
                logger.exception("worker de esperas falhou")
            await asyncio.sleep(30)

    tarefas = [asyncio.create_task(bot.rodar()), asyncio.create_task(acordar_esperas())]
    await parar.wait()

    logger.info("parando…")
    bot.parar()
    for t in tarefas:
        t.cancel()
    await bot.fechar()
    s = bot.stats
    logger.info(
        "parado: %d turnos · %d bolhas · %d conversas · %d handoffs · %d erros",
        s.turnos,
        s.bolhas,
        len(s.chats),
        s.handoffs,
        s.erros,
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Sobe um agente da ZOI no Telegram.")
    ap.add_argument("tenant", help="id do tenant (uma pasta em tenants/)")
    ap.add_argument("--tenants", default="tenants", type=Path)
    ap.add_argument("--dados", default=Path(".dados"), type=Path)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    carregar_env(Path(__file__).parent / ".env")
    return asyncio.run(rodar(args.tenant, dados=args.dados, tenants_dir=args.tenants))


if __name__ == "__main__":
    raise SystemExit(main())
