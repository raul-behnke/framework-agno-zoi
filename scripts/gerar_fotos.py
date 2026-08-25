"""Gera as fotos do estoque fictício com a API de imagens da OpenAI.

    OPENAI_IMAGE_KEY=sk-... uv run python scripts/gerar_fotos.py
    OPENAI_IMAGE_KEY=sk-... uv run python scripts/gerar_fotos.py SUV-001

Sem argumento gera o catálogo inteiro; com códigos, só eles. Já existente é
pulado — regerar custa dinheiro, e o script vai ser rodado de novo toda vez
que um veículo novo entrar no catálogo.

`quality="low"` de propósito: a foto é ilustração de um pátio fictício numa
demo, não material de anúncio. Low custa ~10x menos que high e a diferença
não aparece numa bolha de WhatsApp.
"""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

import httpx
import yaml

RAIZ = Path(__file__).resolve().parent.parent
CATALOGO = RAIZ / "tenants" / "zoi_veiculos" / "catalog.yaml"
DESTINO = RAIZ / "tenants" / "zoi_veiculos" / "fotos"
URL = "https://api.openai.com/v1/images/generations"


def prompt_de(v: dict) -> str:
    """Descrição do veículo para o gerador.

    Ângulo e enquadramento fixos em todos: um pátio onde cada carro é
    fotografado de um jeito parece colagem de anúncio de terceiro, não
    estoque de uma loja só.
    """
    return (
        f"Fotografia publicitária de um {v['marca']} {v['modelo']} {v['versao']} "
        f"{v['ano']}, cor {v['cor']}, em ótimo estado de conservação. "
        "Veículo parado no pátio de uma revenda de seminovos, visto de três "
        "quartos frontal, carroceria inteira no enquadramento. Luz natural de "
        "fim de tarde, fundo limpo e desfocado, sem pessoas, sem texto, sem "
        "marca d'água, sem placa legível. Enquadramento horizontal."
    )


def gerar(cliente: httpx.Client, v: dict, destino: Path) -> str:
    # JPEG vem da própria API: o PNG do padrão sai com ~2 MB por foto, 39 MB
    # no total, e o Telegram reencoda para JPEG na hora de mandar de qualquer
    # jeito. Pedir jpeg aqui evita carregar 35 MB de peso morto no repo.
    r = cliente.post(
        URL,
        json={
            "model": "gpt-image-1",
            "prompt": prompt_de(v),
            "size": "1536x1024",
            "quality": "low",
            "output_format": "jpeg",
            "output_compression": 82,
            "n": 1,
        },
    )
    if r.status_code != 200:
        return f"HTTP {r.status_code}: {r.text[:200]}"
    dados = r.json()["data"][0]
    destino.write_bytes(base64.b64decode(dados["b64_json"]))
    return "ok"


def main(argv: list[str]) -> int:
    chave = os.getenv("OPENAI_IMAGE_KEY") or os.getenv("OPENAI_API_KEY")
    if not chave:
        print("falta OPENAI_IMAGE_KEY (ou OPENAI_API_KEY) no ambiente", file=sys.stderr)
        return 2

    veiculos = yaml.safe_load(CATALOGO.read_text(encoding="utf-8"))["veiculos"]
    if argv:
        alvos = {c.upper() for c in argv}
        veiculos = [v for v in veiculos if v["codigo"].upper() in alvos]
        if not veiculos:
            print(f"nenhum veículo casa com {sorted(alvos)}", file=sys.stderr)
            return 2

    DESTINO.mkdir(parents=True, exist_ok=True)
    falhas = 0
    with httpx.Client(
        headers={"Authorization": f"Bearer {chave}"},
        timeout=httpx.Timeout(180.0),
    ) as cliente:
        for i, v in enumerate(veiculos, 1):
            saida = DESTINO / f"{v['codigo']}.jpg"
            if saida.exists():
                print(f"[{i}/{len(veiculos)}] {v['codigo']} já existe, pulando")
                continue
            status = gerar(cliente, v, saida)
            if status != "ok":
                falhas += 1
            tamanho = f"{saida.stat().st_size // 1024} KB" if saida.exists() else "—"
            print(f"[{i}/{len(veiculos)}] {v['codigo']} {v['nome']}: {status} {tamanho}")

    print(f"\n{len(veiculos) - falhas}/{len(veiculos)} geradas em {DESTINO}")
    return 1 if falhas else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
