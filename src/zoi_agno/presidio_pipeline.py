import logging
import os
import re
from typing import Any

# Fallback-first approach: regex cobre BR entities que o Presidio NER
# pode perder. Presidio é called em cima do regex.

_LOG = logging.getLogger(__name__)


def _env_truthy(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


_CPF_RE = re.compile(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b")
_CNPJ_RE = re.compile(r"\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b")
_RG_RE = re.compile(r"\b\d{2}\.\d{3}\.\d{3}-[\dXx]\b")
_CEP_RE = re.compile(r"\b\d{5}-?\d{3}\b")
_PHONE_BR_RE = re.compile(r"\(?\d{2}\)?\s?9?\d{4}-?\d{4}")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_CC_RE = re.compile(r"\b(?:\d{4}[\s-]?){3}\d{4}\b")
_PLATE_RE = re.compile(r"\b[A-Z]{3}-?\d[A-Z0-9]\d{2}\b")


_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("CPF", _CPF_RE),
    ("CNPJ", _CNPJ_RE),
    ("RG", _RG_RE),
    ("CEP", _CEP_RE),
    ("PHONE", _PHONE_BR_RE),
    ("EMAIL", _EMAIL_RE),
    ("CC", _CC_RE),
    ("PLATE", _PLATE_RE),
]


class PresidioPipeline:
    """PII redaction. Regex layer (BR entities) is always on; the spaCy NER
    layer is OPT-IN.

    Why opt-in (2026-06-19): the default Presidio ``AnalyzerEngine`` loads
    ``en_core_web_lg`` (~760MB resident, the dominant adapter footprint behind
    the VPS OOMs) but ``redact()`` only ever calls ``analyze(language="pt")``,
    which the en-only engine rejects with ``ValueError`` (verified) — so the
    NER output is *always discarded* and the 760MB is pure dead weight. Worse,
    actually wiring pt NER would redact PERSON tokens (the lead's name) before
    the LLM and break name personalisation. So the engine is skipped by default
    (no behaviour change — pt NER never contributed) and gated behind
    ``ZOI_PRESIDIO_SPACY`` for callers who add a pt model or use en analysis.
    """

    def __init__(self, *, load_spacy: bool | None = None) -> None:
        self.analyzer: Any = None
        self.anonymizer: Any = None
        if load_spacy is None:
            load_spacy = _env_truthy("ZOI_PRESIDIO_SPACY", default=False)
        if not load_spacy:
            # Regex-only mode (default). Skips the ~760MB en_core_web_lg load
            # whose pt NER output is discarded anyway.
            return
        try:
            from presidio_analyzer import AnalyzerEngine
            from presidio_anonymizer import AnonymizerEngine

            self.analyzer = AnalyzerEngine(default_score_threshold=0.5)
            self.anonymizer = AnonymizerEngine()  # type: ignore[no-untyped-call]
        except ImportError:
            # Presidio package ausente — regex-only mode (dev sem extras).
            pass
        except (SystemExit, Exception) as e:  # noqa: BLE001
            # spaCy model ausente, NLP engine init falhou, ou outro erro
            # de carregamento. Não derrubar serviço; degradar pra regex-only.
            # SystemExit em particular vem de spacy.cli.download quando
            # nem pip nem uv estão no PATH do venv.
            _LOG.warning(
                "presidio_init_failed err_class=%s msg=%s — falling back to regex-only PII redaction",
                type(e).__name__,
                str(e)[:200],
            )

    def redact(self, text: str) -> tuple[str, dict[str, str]]:
        """Retorna (redacted_text, mapping) — mapping permite rehydrate."""
        mapping: dict[str, str] = {}
        out = text
        for label, pattern in _PATTERNS:
            counter = 0

            def _replacer(m: re.Match[str], lbl: str = label) -> str:
                nonlocal counter
                raw = m.group(0)
                counter += 1
                token = f"<{lbl}_{counter}>"
                mapping[token] = raw
                return token

            out = pattern.sub(_replacer, out)

        if self.analyzer:
            # Entities pt-BR suportadas: PERSON, LOCATION, DATE_TIME, etc
            try:
                results = self.analyzer.analyze(text=out, language="pt")
                if results:
                    anon = self.anonymizer.anonymize(text=out, analyzer_results=results)
                    out = anon.text
            except ValueError:
                # Presidio registry may not support pt — regex layer already handled BR entities.
                pass
        return out, mapping

    def rehydrate(self, text: str, mapping: dict[str, str]) -> str:
        return rehydrate_pii(text, mapping)


def rehydrate_pii(text: str, mapping: dict[str, str] | None) -> str:
    """Token ``<EMAIL_1>`` → valor real. Usado nos egress first-party (tools GHL,
    crm_sync) que precisam do dado cru — as superfícies do LLM seguem com token."""
    if not mapping:
        return text
    out = text
    for token, raw in mapping.items():
        out = out.replace(token, raw)
    return out
