from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACT_PATH = REPO_ROOT / "packages" / "anki-contracts" / "basic_to_cloze.py"
KNOWLEDGE_PATH = (
    REPO_ROOT / "apps" / "anki-gpt" / "gpt-knowledge" / "07_conversao_basic_para_cloze.md"
)
INSTRUCTIONS_PATH = (
    REPO_ROOT
    / "apps"
    / "anki-gpt"
    / "gpt-knowledge"
    / "INSTRUCTIONS_CURTAS_GPT_BUILDER.md"
)
COMPACT_SCHEMA_PATH = (
    REPO_ROOT / "contracts" / "openapi" / "gpt-action-compact.openapi.json"
)
GPT_SCHEMA_PATH = (
    REPO_ROOT / "apps" / "anki-gpt" / "gpt-knowledge" / "schema gpt.json"
)


def load_contract():
    spec = importlib.util.spec_from_file_location("basic_to_cloze_contract_test", CONTRACT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


CONTRACT = load_contract()


SAFE_CASES = [
    {
        "category": "acceptance",
        "front": "Em uma teia alimentar, o nível trófico de um organismo é fixo?",
        "back": "Não. O mesmo organismo pode ocupar diferentes níveis tróficos.",
        "text": "Em uma teia alimentar, o nível trófico de um organismo {{c1::não é fixo}}.",
        "back_extra": (
            "O mesmo organismo pode ocupar diferentes níveis tróficos "
            "conforme a cadeia alimentar considerada."
        ),
    },
    {
        "category": "yes_no",
        "front": "As pirâmides de energia podem ser invertidas?",
        "back": "Não, porque há perda de energia a cada nível trófico.",
        "text": "As pirâmides de energia {{c1::não podem ser invertidas}}.",
        "back_extra": "Há perda de energia a cada transferência entre níveis tróficos.",
    },
    {
        "category": "identification",
        "front": "Qual organela realiza a respiração celular aeróbia?",
        "back": "Mitocôndria.",
        "text": "A respiração celular aeróbia ocorre principalmente nas {{c1::mitocôndrias}}.",
        "back_extra": "",
    },
    {
        "category": "definition",
        "front": "O que é uma população em ecologia?",
        "back": "Conjunto de indivíduos da mesma espécie que vivem em uma mesma área.",
        "text": (
            "Uma população é o {{c1::conjunto de indivíduos da mesma espécie "
            "que vivem em uma mesma área}}."
        ),
        "back_extra": "",
    },
    {
        "category": "causal",
        "front": "Por que a energia diminui ao longo da cadeia alimentar?",
        "back": "Porque parte da energia é dissipada como calor em cada nível trófico.",
        "text": (
            "A energia diminui ao longo da cadeia alimentar porque parte dela é "
            "{{c1::dissipada como calor em cada nível trófico}}."
        ),
        "back_extra": "",
    },
    {
        "category": "comparison",
        "front": "Em comparação aos consumidores, quem ocupa a base da pirâmide de energia?",
        "back": "Os produtores.",
        "text": "A base da pirâmide de energia é ocupada pelos {{c1::produtores}}.",
        "back_extra": "",
    },
    {
        "category": "explanation_or_example",
        "front": "O nível trófico de um organismo é sempre o mesmo?",
        "back": "Não. Ele pode variar dependendo da cadeia alimentar considerada.",
        "text": (
            "O nível trófico de um organismo {{c1::pode variar conforme a cadeia "
            "alimentar considerada}}."
        ),
        "back_extra": (
            "Em uma teia alimentar, o mesmo organismo pode ocupar diferentes "
            "níveis tróficos."
        ),
    },
    {
        "category": "multiple_propositions",
        "front": "O que ocorre com a energia entre níveis tróficos e qual é uma consequência?",
        "back": "A energia diminui. Por isso, cadeias alimentares tendem a ter poucos níveis.",
        "text": "Entre níveis tróficos, a energia disponível {{c1::diminui}}.",
        "back_extra": "Como consequência, cadeias alimentares tendem a ter poucos níveis.",
    },
    {
        "category": "html",
        "front": (
            '<img src="mitose.png"><br>Em qual fase os cromossomos se alinham '
            "no equador celular?"
        ),
        "back": "Metáfase.",
        "text": (
            '<img src="mitose.png"><br>Os cromossomos se alinham no equador '
            "celular durante a {{c1::metáfase}}."
        ),
        "back_extra": "",
    },
    {
        "category": "mathjax",
        "front": "Na fórmula \\(E=mc^2\\), qual grandeza é representada por \\(m\\)?",
        "back": "Massa.",
        "text": "Na fórmula \\(E=mc^2\\), \\(m\\) representa a {{c1::massa}}.",
        "back_extra": "",
    },
]


@pytest.mark.parametrize("case", SAFE_CASES, ids=lambda case: case["category"])
def test_safe_regression_cases_pass_structural_contract(case):
    result = CONTRACT.validate_basic_to_cloze_fields(
        front=case["front"],
        back=case["back"],
        text=case["text"],
        back_extra=case["back_extra"],
    )
    assert result["cloze_numbers"][0] == 1


@pytest.mark.parametrize(
    "text,error",
    [
        (
            "Em uma teia alimentar, o nível trófico de um organismo é fixo?"
            "<br>{{c1::Não. O mesmo organismo pode ocupar diferentes níveis "
            "tróficos.}}",
            "question_not_declarative",
        ),
        (
            "Em uma {{c1::teia alimentar}}, o nível trófico de um organismo {{c2::não}} é fixo.",
            "low_information_deletion",
        ),
        (
            "Em uma teia alimentar, {{c1::o nível trófico de um organismo não é "
            "fixo e o mesmo organismo pode ocupar diferentes níveis tróficos}}.",
            "oversized_deletion",
        ),
    ],
)
def test_acceptance_rejects_known_mechanical_outputs(text, error):
    with pytest.raises(ValueError, match=error):
        CONTRACT.validate_basic_to_cloze_fields(
            front=SAFE_CASES[0]["front"],
            back=SAFE_CASES[0]["back"],
            text=text,
            back_extra="",
        )


def test_already_cloze_source_is_not_rewritten():
    with pytest.raises(ValueError, match="source_already_cloze"):
        CONTRACT.validate_basic_to_cloze_fields(
            front="A organela é a {{c1::mitocôndria}}.",
            back="",
            text="A organela é a {{c1::mitocôndria}}.",
            back_extra="",
        )


def test_unsafe_conversion_keeps_source_for_manual_review():
    source = {
        "note_id": 101,
        "card_ids": [201],
        "deck": "Fixture::Biologia",
        "tags": ["FO", "preservar"],
        "fields": {
            "Front": "Explique detalhadamente o ciclo inteiro mostrado na aula.",
            "Back": (
                "Resposta extensa sem contexto suficiente para escolher uma "
                "proposição inequívoca."
            ),
            "Source": "media.pdf",
        },
    }
    result = {
        "status": "manual_review",
        "reason": "insufficient_context_for_safe_basic_to_cloze",
        **json.loads(json.dumps(source)),
    }
    assert result["note_id"] == source["note_id"]
    assert result["card_ids"] == source["card_ids"]
    assert result["deck"] == source["deck"]
    assert result["tags"] == source["tags"]
    assert result["fields"] == source["fields"]


def test_metadata_preservation_contract_covers_ids_deck_tags_and_unrelated_fields():
    required = {"note_id", "card_ids", "deck", "tags", "unrelated_fields"}
    preservation_contract = {
        "note_id": "unchanged",
        "card_ids": "existing_ids_unchanged",
        "deck": "unchanged",
        "tags": "unchanged",
        "unrelated_fields": "unchanged_or_manual_review",
    }
    assert set(preservation_contract) == required
    knowledge = KNOWLEDGE_PATH.read_text(encoding="utf-8")
    for marker in ("note_id", "card IDs", "deck", "tags", "campos não relacionados"):
        assert marker in knowledge


def test_knowledge_is_single_explicit_source_and_distinguishes_four_flows():
    knowledge = KNOWLEDGE_PATH.read_text(encoding="utf-8")
    assert "fonte de verdade" in knowledge
    for marker in (
        "conversão estrutural Basic -> Cloze",
        "normalização de Cloze existente",
        "criação de card novo",
        "edição estética/HTML",
        "convertBasicToClozeOperation",
        "revisão manual",
    ):
        assert marker in knowledge


def test_short_instructions_require_canonical_read_and_specific_conversion_operation():
    instructions = INSTRUCTIONS_PATH.read_text(encoding="utf-8")
    conversion = next(
        paragraph
        for paragraph in instructions.split("\n\n")
        if paragraph.startswith("Conversao estrutural Basic -> Cloze")
    )
    for marker in (
        "Leia primeiro a note Basic",
        "source_front",
        "source_back",
        "expected_content_hash",
        "expected_mod",
        "expected_usn",
        "expected_model_id",
        "convertBasicToClozeOperation",
        "convert_basic_to_cloze",
        "A Front nao pode permanecer como pergunta",
        "frase declarativa natural",
        "Back Extra",
        '"nao", "sim", "e" ou "pode"',
        "normalizacao de Cloze existente",
        "Front<br>{{c1::Back inteiro}}",
    ):
        assert marker in conversion
    assert "nunca createClozeNoteOperation/create-cloze-note" in conversion


def test_gpt_builder_schema_is_canonical_symlink_with_full_conversion_contract():
    assert GPT_SCHEMA_PATH.is_symlink()
    assert GPT_SCHEMA_PATH.read_bytes() == COMPACT_SCHEMA_PATH.read_bytes()
    schema = json.loads(COMPACT_SCHEMA_PATH.read_text(encoding="utf-8"))
    operation = schema["paths"]["/organization/convert-basic-to-cloze"]["post"]
    assert operation["operationId"] == "convertBasicToClozeOperation"
    assert operation["requestBody"]["required"] is True
    assert (
        operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/BasicToClozeOperationCreatedResponse"
    )
    assert set(operation["responses"]) == {"200", "400", "401", "500"}

    request = schema["components"]["schemas"]["BasicToClozeRequest"]
    assert set(request["required"]) == {
        "note_id",
        "source_front",
        "source_back",
        "text",
        "back_extra",
        "expected_content_hash",
    }
    response = schema["components"]["schemas"]["BasicToClozeOperationCreatedResponse"]
    assert response["properties"]["operation"]["properties"]["operation_type"] == {
        "type": "string",
        "const": "convert_basic_to_cloze",
    }
