"""
the workspace turns on-screen choices into ordinary Ordixa profiles: every
document must survive TOML and validate with the engine's own loaders
"""

import tomllib

import pytest
from workspace_helpers import EXAMPLES

from app.ordixa.profile import ProfileError, load_profile
from app.ordixa.tabular_profile import load_table_profile, validate_table_profile
from app.workspace import preview, profiles, toml_writer

PROFILES = sorted((EXAMPLES / "profiles").glob("*.toml"))
INVENTORY = EXAMPLES / "exports" / "inventory"
DEMO = EXAMPLES / "exports" / "demo"


@pytest.mark.parametrize("path", PROFILES, ids=lambda path: path.stem)
def test_every_example_profile_round_trips_through_the_writer(path, tmp_path):
    document = tomllib.loads(path.read_text("utf-8"))
    text = toml_writer.dumps(document)
    assert tomllib.loads(text) == document

    # the rewritten profile means exactly what the original means
    copy = tmp_path / path.name
    copy.write_text(text, "utf-8")
    if "workflow" in document:
        original, rewritten = load_table_profile(path), load_table_profile(copy)
    else:
        original, rewritten = load_profile(path), load_profile(copy)
    ignored = {"source_path", "sha256"}
    assert {k: v for k, v in vars(original).items() if k not in ignored} == {
        k: v for k, v in vars(rewritten).items() if k not in ignored
    }


def test_writer_quotes_awkward_headers_and_values():
    document = {
        "schema_version": 1,
        "profile": {"name": "odd", "description": 'quote " back\\slash\nnew\tline é'},
        "columns": {
            "price": {"source": "Price (USD) [incl. tax]", "type": "decimal"},
            "note": {"source": "Note\u0007bell"},
        },
        "maps": {"Partially Paid": "partial", "a.b": "dotted", "": None},
        "empty": {},
    }
    text = toml_writer.dumps(document)
    assert tomllib.loads(text) == toml_writer.strip_none(document)


def test_writer_refuses_values_a_profile_cannot_hold():
    with pytest.raises(toml_writer.TomlWriteError):
        toml_writer.dumps({"amount": 1.5})
    with pytest.raises(toml_writer.TomlWriteError):
        toml_writer.dumps({"": "blank key"})


def test_engine_validation_reads_the_document_back_from_toml():
    document = profiles.sample_document("inventory-compare")
    text, profile = profiles.engine_validate("compare", document)
    assert profile.key == ("sku",)
    assert validate_table_profile(tomllib.loads(text)).columns == profile.columns


def test_engine_validation_refuses_the_wrong_workflow():
    document = profiles.sample_document("inventory-clean")
    with pytest.raises(ProfileError):
        profiles.engine_validate("compare", document)


def headers_of(*paths):
    return {
        f"file{i}": preview.describe(path)["header"] for i, path in enumerate(paths)
    }


def test_plain_language_problems_come_before_engine_validation():
    document = profiles.sample_document("inventory-compare")
    document["table"]["key"] = []
    document["table"]["date_formats"] = []
    document["amounts"]["currency"] = "dollars"
    document["profile"]["name"] = "Weekly Inventory!"
    problems, text, profile = profiles.validate(
        "compare", document, headers_of(INVENTORY / "inventory-before.csv")
    )
    assert text is None and profile is None
    messages = " ".join(item["message"] for item in problems)
    assert "identifies each record" in messages
    assert "never guesses" in messages
    assert "three-letter currency code" in messages
    assert "short name" in messages
    # no profile path syntax such as table.key leaks into these messages
    assert "table.key" not in messages


def test_a_key_column_must_be_text_or_a_code():
    document = profiles.sample_document("inventory-compare")
    document["columns"]["sku"]["type"] = "integer"
    problems, text, _ = profiles.validate("compare", document, {})
    assert text is None
    assert any("007 and 7" in item["message"] for item in problems)


def test_a_mapped_column_missing_from_the_file_is_reported_by_name():
    document = profiles.sample_document("inventory-clean")
    document["columns"]["sku"]["source"] = "Stock Code"
    problems, text, _ = profiles.validate(
        "clean", document, headers_of(INVENTORY / "inventory-before.csv")
    )
    assert text is None
    assert any("'Stock Code'" in item["message"] for item in problems)


def test_engine_problems_are_passed_through_when_the_draft_looks_fine():
    document = profiles.sample_document("inventory-clean")
    document["table"]["date_formats"] = ["%Y-%m-%d %Q"]
    problems, text, _ = profiles.validate("clean", document, {})
    assert text is None
    assert any(item["message"].startswith("Ordixa cannot use") for item in problems)


def test_suggested_clean_settings_are_valid_and_sensible():
    description = preview.describe(INVENTORY / "inventory-before.csv")
    document, questions = profiles.suggest_table("clean", description, "inventory")
    _, profile = profiles.engine_validate("clean", document)
    types = {column.source: column.type for column in profile.columns}
    assert types["Item SKU"] == "identifier"
    assert types["Unit Price"] == "decimal"
    assert types["Qty On Hand"] == "integer"
    assert types["Last Counted"] == "date"
    assert profile.key == ("item_sku",)
    assert {"N/A", "-"} <= set(profile.blank_values)
    # 10/05/2026 could be read either way, so the person is asked
    assert ["%m/%d/%Y", "%d/%m/%Y"] in questions
    assert "%m/%d/%Y" not in profile.date_formats
    assert "%d/%m/%Y" not in profile.date_formats


def test_suggested_reconciliation_settings_are_valid_and_map_values():
    descriptions = {
        "orders": preview.describe(DEMO / "orders_2026-10.csv"),
        "payments": preview.describe(DEMO / "payments_2026-10.xlsx"),
        "customers": preview.describe(DEMO / "customers.csv"),
    }
    values = {
        "transaction_type": preview.distinct_values(
            [DEMO / "payments_2026-10.xlsx"], "Type"
        ),
        "status": preview.distinct_values([DEMO / "payments_2026-10.xlsx"], "Status"),
    }
    document, _ = profiles.suggest_reconcile(descriptions, values, "harbor")
    document = profiles.for_run(
        "reconcile",
        document,
        {
            "orders": ["orders_2026-10.csv"],
            "payments": ["payments_2026-10.xlsx"],
            "customers": ["customers.csv"],
        },
    )
    _, profile = profiles.engine_validate("reconcile", document)
    payments = profile.datasets["payments"]
    assert payments.columns["order_id"] == ("Order Reference",)
    assert payments.columns["amount"] == ("Amount",)
    assert payments.value_maps["transaction_type"]["refund"] == "refund"
    assert "failed" in payments.excluded_statuses
    assert profile.datasets["customers"].columns["name"] == ("First Name", "Last Name")
    assert profile.datasets["orders"].files == ("orders_2026-10.csv",)


def test_run_documents_point_only_at_the_added_files():
    document = profiles.sample_document("harbor-lane")
    prepared = profiles.for_run(
        "reconcile", document, {"orders": ["a.csv"], "payments": ["b.xlsx"]}
    )
    assert prepared["datasets"]["orders"]["files"] == ["a.csv"]
    assert "customers" not in prepared["datasets"]
    # the screen's own copy is never changed
    assert document["datasets"]["orders"]["files"] == ["orders*.csv", "orders*.xlsx"]


def test_the_cli_reports_an_unusable_date_directive_as_a_profile_error(tmp_path):
    # some platforms' strftime rejects unknown directives outright; that used
    # to escape validation and end the command with an unexpected error
    from app.ordixa.cli import main as cli_main

    text = (EXAMPLES / "profiles" / "inventory-clean.toml").read_text("utf-8")
    broken = tmp_path / "broken.toml"
    broken.write_text(
        text.replace(
            'date_formats = ["%Y-%m-%d", "%m/%d/%Y"]', 'date_formats = ["%Y-%m-%d %Q"]'
        ),
        "utf-8",
    )
    assert cli_main(["check-profile", "--profile", str(broken)]) == 2
