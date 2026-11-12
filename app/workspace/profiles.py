"""
turns the choices made on screen into an ordinary Ordixa profile

the screen edits a profile document directly: the same structure a TOML
profile has. Before anything is checked or run, the document is written as
TOML, read back, and validated by the engine's own profile loaders
(validate_table_profile and validate_profile_document), so a profile saved
from the workspace behaves identically with the command line.

this module also suggests a first draft from a file's columns, and checks a
draft for the common mistakes in plain language before the engine's more
technical validation runs
"""

import hashlib
import re
import tomllib
from pathlib import Path

from app.ordixa import __version__
from app.ordixa.profile import (
    DATASET_FIELDS,
    PROFILE_NAME_PATTERN,
    ProfileError,
    validate_profile_document,
)
from app.ordixa.tabular_profile import (
    COLUMN_NAME_PATTERN,
    is_table_profile_document,
    validate_table_profile,
)
from app.workspace import toml_writer

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = PROJECT_ROOT / "examples" / "ordixa"

# fictional sample sets that ship with the repository
SAMPLES = {
    "inventory-clean": {
        "workflow": "clean",
        "title": "Sample: tidy an inventory list",
        "description": (
            "A fictional general store's inventory export with stray spaces, "
            "placeholder text, a missing price, and two rows sharing one SKU."
        ),
        "profile": EXAMPLES / "profiles" / "inventory-clean.toml",
        "files": {"input": ["exports/inventory/inventory-before.csv"]},
    },
    "inventory-compare": {
        "workflow": "compare",
        "title": "Sample: what changed in the inventory",
        "description": (
            "Two weeks of the same fictional inventory: new products, removed "
            "products, price and stock changes, and rows that need fixing."
        ),
        "profile": EXAMPLES / "profiles" / "inventory-compare.toml",
        "files": {
            "before": ["exports/inventory/inventory-before.csv"],
            "after": ["exports/inventory/inventory-after.csv"],
        },
    },
    "harbor-lane": {
        "workflow": "reconcile",
        "title": "Sample: match payments to orders",
        "description": (
            "A fictional shop's October orders, card payments (an Excel file), "
            "and customers, including refunds, a part payment, and an overpayment."
        ),
        "profile": EXAMPLES / "profiles" / "harbor-lane.toml",
        "files": {
            "orders": ["exports/demo/orders_2026-10.csv"],
            "payments": ["exports/demo/payments_2026-10.xlsx"],
            "customers": ["exports/demo/customers.csv"],
        },
    },
}

TYPE_LABELS = {
    "text": "Text",
    "identifier": "Code or ID",
    "decimal": "Money or exact number",
    "integer": "Whole number",
    "date": "Date",
}
DATE_FORMAT_LABELS = {
    "%Y-%m-%d": "2026-03-04 (year-month-day)",
    "%Y-%m-%d %H:%M:%S": "2026-03-04 14:30:00",
    "%Y-%m-%d %H:%M:%S %z": "2026-03-04 14:30:00 -0500",
    "%Y-%m-%dT%H:%M:%S": "2026-03-04T14:30:00",
    "%Y-%m-%dT%H:%M:%S%z": "2026-03-04T14:30:00-0500",
    "%Y/%m/%d": "2026/03/04",
    "%m/%d/%Y": "03/04/2026 (month first)",
    "%d/%m/%Y": "04/03/2026 (day first)",
    "%m/%d/%Y %H:%M": "03/04/2026 14:30 (month first)",
    "%d/%m/%Y %H:%M": "04/03/2026 14:30 (day first)",
    "%d.%m.%Y": "04.03.2026",
    "%b %d, %Y": "Mar 04, 2026",
    "%d %b %Y": "04 Mar 2026",
}
KEY_HEADER = re.compile(
    r"(^|[\s_-])(sku|id|code|number|no|num|ref|reference|key)([\s_.#-]|$)", re.I
)
FIELD_LABELS = {
    "customer_id": "Customer number",
    "name": "Customer name",
    "email": "Email",
    "phone": "Phone",
    "order_id": "Order number",
    "date": "Order date",
    "total": "Order total",
    "currency": "Currency",
    "payment_id": "Payment or transaction ID",
    "payment_date": "Payment date",
    "amount": "Amount",
    "transaction_type": "Kind of transaction",
    "status": "Payment status",
}
# header spellings tried, in order, when suggesting a reconciliation mapping
FIELD_HEADERS = {
    "orders": {
        "order_id": [
            "order id",
            "order number",
            "order no",
            "order #",
            "order",
            "name",
        ],
        "customer_id": ["customer id", "customer number", "customer no", "customer"],
        "date": ["order date", "created at", "created", "date"],
        "total": ["total", "grand total", "order total", "amount"],
        "currency": ["currency"],
    },
    "payments": {
        "payment_id": [
            "transaction id",
            "payment id",
            "payment ref",
            "payment reference",
            "reference",
            "id",
        ],
        "order_id": [
            "order reference",
            "order id",
            "order number",
            "order no",
            "order",
        ],
        "payment_date": ["payment date", "paid on", "date"],
        "amount": ["amount", "gross", "net", "total"],
        "transaction_type": ["type", "transaction type", "kind"],
        "status": ["status", "state", "payment status"],
        "currency": ["currency"],
    },
    "customers": {
        "customer_id": ["customer id", "customer number", "customer no", "id"],
        "name": ["name", "full name", "customer name"],
        "email": ["email", "email address"],
        "phone": ["phone", "phone number", "telephone"],
    },
}
TRANSACTION_WORDS = {
    "payment": "payment",
    "sale": "payment",
    "charge": "payment",
    "capture": "payment",
    "refund": "refund",
    "return": "refund",
    "adjustment": "adjustment",
    "correction": "adjustment",
}
STATUS_WORDS = {
    "paid": "paid",
    "completed": "paid",
    "complete": "paid",
    "succeeded": "paid",
    "success": "paid",
    "settled": "paid",
    "partially paid": "partial",
    "partial": "partial",
    "pending": "pending",
    "processing": "pending",
    "refunded": "refunded",
}
EXCLUDED_STATUS_WORDS = {
    "failed",
    "voided",
    "void",
    "declined",
    "cancelled",
    "canceled",
}


def fold(text):
    """
    compares header and value spellings without case or extra spaces
    :param text: text
    :returns: folded text
    """
    return " ".join(str(text).replace("_", " ").split()).casefold()


def slug(text, fallback="settings"):
    """
    builds a profile name from a file name
    :param text: any text
    :param fallback: name used when nothing usable is left
    :returns: lowercase name with hyphens
    """
    name = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:56]
    return name if name and PROFILE_NAME_PATTERN.match(name) else fallback


def column_name(header, taken):
    """
    builds an output column name such as unit_price from a header
    :param header: source header
    :param taken: names already used
    :returns: unused lowercase name
    """
    name = re.sub(r"[^a-z0-9]+", "_", header.lower()).strip("_")[:60]
    if not name or not name[0].isalpha():
        name = f"column_{name}" if name else "column"
    candidate, number = name, 2
    while candidate in taken:
        candidate = f"{name}_{number}"
        number += 1
    taken.add(candidate)
    return candidate


# serialization and validation


def to_toml(document):
    """
    writes a profile document as commented TOML
    :param document: profile document
    :returns: TOML text
    """
    return toml_writer.dumps(
        document,
        header=[
            "Ordixa profile saved from the local workspace.",
            f"Written by Ordixa {__version__}; use it with python -m app.ordixa too.",
        ],
    )


def parse_toml(text):
    """
    reads profile text the way the command line does
    :param text: TOML text
    :returns: document
    """
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ProfileError([f"TOML syntax error: {error}"]) from error


def engine_validate(workflow, document):
    """
    validates a document with the engine's own loaders, through TOML

    the document is serialized, read back, and validated exactly as
    load_profile or load_table_profile would read a file on disk
    :param workflow: clean, compare, or reconcile
    :param document: profile document
    :returns: tuple of TOML text and validated profile
    """
    try:
        text = to_toml(document)
    except toml_writer.TomlWriteError as error:
        raise ProfileError([str(error)]) from error
    parsed = parse_toml(text)
    sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if workflow == "reconcile":
        profile = validate_profile_document(parsed, "<workspace>", sha256)
    else:
        profile = validate_table_profile(parsed, "<workspace>", sha256)
        if profile.kind != workflow:
            raise ProfileError(
                [
                    f"workflow.kind: these settings are for {profile.kind}, not {workflow}"
                ]
            )
    return text, profile


def document_workflow(document):
    """
    names the workflow a loaded profile is for
    :param document: parsed TOML document
    :returns: clean, compare, or reconcile
    """
    if is_table_profile_document(document):
        return (document.get("workflow") or {}).get("kind")
    return "reconcile"


def problem(message, where="", level="blocking"):
    """
    builds one plain-language problem
    :param message: what is wrong and how to fix it
    :param where: the setting it concerns, for highlighting on screen
    :param level: blocking or warning
    :returns: dictionary
    """
    return {"level": level, "where": where, "message": message}


def table_problems(workflow, document, headers):
    """
    checks clean/compare settings for common mistakes, in plain language
    :param workflow: clean or compare
    :param document: profile document
    :param headers: header names of every input file
    :returns: list of problems
    """
    problems = []
    meta = document.get("profile") or {}
    if not PROFILE_NAME_PATTERN.match(str(meta.get("name", ""))):
        problems.append(
            problem(
                "Give these settings a short name using lowercase letters, numbers, "
                "and hyphens, such as weekly-inventory.",
                "profile.name",
            )
        )
    columns = document.get("columns") or {}
    key = (document.get("table") or {}).get("key") or []
    if not columns:
        problems.append(
            problem("Choose at least one column to keep.", "columns"),
        )
    sources = {}
    for name, column in columns.items():
        label = column.get("source") or name
        if not COLUMN_NAME_PATTERN.match(name):
            problems.append(
                problem(
                    f"The output name for {label!r} must start with a letter and use "
                    "only lowercase letters, numbers, and underscores.",
                    f"columns.{name}",
                )
            )
        source = column.get("source", "")
        if source in sources:
            problems.append(
                problem(
                    f"{source!r} is used twice ({sources[source]} and {name}).",
                    f"columns.{name}",
                )
            )
        sources[source] = name
        for role, header in headers.items():
            if header and source not in header:
                need = (
                    "it is required"
                    if column.get("required") or name in key
                    else "its values will be blank"
                )
                problems.append(
                    problem(
                        f"The {role} file has no column called {source!r}; {need}.",
                        f"columns.{name}",
                        "blocking" if need == "it is required" else "warning",
                    )
                )
    table = document.get("table") or {}
    for name in key:
        column = columns.get(name)
        if column and column.get("type") not in ("text", "identifier"):
            problems.append(
                problem(
                    f"{column.get('source', name)!r} identifies each record, so it "
                    "must be a Code or ID (or Text) column. Numbers would merge "
                    "codes such as 007 and 7.",
                    f"columns.{name}",
                )
            )
    if workflow == "compare" and not key:
        problems.append(
            problem(
                "Choose the column that identifies each record, such as a SKU or "
                "product code. Ordixa matches the two versions by it, never by row "
                "position.",
                "table.key",
            )
        )
    if any(c.get("type") == "date" for c in columns.values()) and not table.get(
        "date_formats"
    ):
        problems.append(
            problem(
                "Say how dates are written in this file. Ordixa never guesses "
                "whether 03/04 means March 4 or April 3.",
                "table.date_formats",
            )
        )
    if any(c.get("type") == "decimal" for c in columns.values()):
        amounts = document.get("amounts") or {}
        if not re.fullmatch(r"[A-Z]{3}", str(amounts.get("currency", ""))):
            problems.append(
                problem(
                    "Enter the three-letter currency code for money columns, such as "
                    "USD, CAD, or EUR.",
                    "amounts.currency",
                )
            )
    return problems


def reconcile_problems(document, headers):
    """
    checks reconciliation settings for common mistakes, in plain language
    :param document: profile document
    :param headers: header names by dataset role, from the first file of each
    :returns: list of problems
    """
    problems = []
    meta = document.get("profile") or {}
    if not PROFILE_NAME_PATTERN.match(str(meta.get("name", ""))):
        problems.append(
            problem(
                "Give these settings a short name using lowercase letters, numbers, "
                "and hyphens, such as monthly-payments.",
                "profile.name",
            )
        )
    amounts = document.get("amounts") or {}
    if not re.fullmatch(r"[A-Z]{3}", str(amounts.get("currency", ""))):
        problems.append(
            problem(
                "Enter the three-letter currency code, such as USD.", "amounts.currency"
            )
        )
    datasets = document.get("datasets") or {}
    for role, fields in DATASET_FIELDS.items():
        dataset = datasets.get(role)
        if dataset is None:
            continue
        columns = dataset.get("columns") or {}
        header = headers.get(role) or []
        for field in sorted(fields["required"]):
            if not columns.get(field):
                problems.append(
                    problem(
                        f"Choose which {role} column holds the "
                        f"{FIELD_LABELS[field].lower()}.",
                        f"datasets.{role}.columns.{field}",
                    )
                )
        for field, source in columns.items():
            for name in source if isinstance(source, list) else [source]:
                if header and name and name not in header:
                    problems.append(
                        problem(
                            f"The {role} file has no column called {name!r}.",
                            f"datasets.{role}.columns.{field}",
                        )
                    )
        if {"date", "payment_date"} & set(columns) and not dataset.get("date_formats"):
            problems.append(
                problem(
                    f"Say how dates are written in the {role} file.",
                    f"datasets.{role}.date_formats",
                )
            )
    payments = datasets.get("payments") or {}
    maps = payments.get("value_maps") or {}
    for field, label in (("transaction_type", "kind"), ("status", "status")):
        if payments.get("columns", {}).get(field) and not maps.get(field):
            problems.append(
                problem(
                    f"Say what each payment {label} means, such as which values are "
                    "refunds.",
                    f"datasets.payments.value_maps.{field}",
                )
            )
    return problems


def engine_problems(error):
    """
    turns engine validation messages into problems the screen can show
    :param error: profile error
    :returns: list of problems
    """
    return [
        problem(f"Ordixa cannot use these settings: {text}", text.split(":", 1)[0])
        for text in error.problems
    ]


def validate(workflow, document, headers):
    """
    checks a draft, first in plain language and then with the engine
    :param workflow: clean, compare, or reconcile
    :param document: profile document
    :param headers: header names by input role
    :returns: tuple of problems, TOML text or none, and profile or none
    """
    if not isinstance(document, dict):
        return [problem("The settings are missing.")], None, None
    try:
        problems = (
            reconcile_problems(document, headers)
            if workflow == "reconcile"
            else table_problems(workflow, document, headers)
        )
    except AttributeError, KeyError, TypeError:
        problems = []
    if any(item["level"] == "blocking" for item in problems):
        return problems, None, None
    try:
        text, profile = engine_validate(workflow, document)
    except ProfileError as error:
        return problems + engine_problems(error), None, None
    return problems, text, profile


def for_run(workflow, document, names):
    """
    points a reconciliation document at exactly the files that were added

    dataset file patterns become the stored file names, so the run reads
    precisely those files; datasets without files are left out when optional
    :param workflow: clean, compare, or reconcile
    :param document: profile document
    :param names: stored file names by role
    :returns: copy of the document ready to validate and run
    """
    document = copy_document(document)
    input_table = document.setdefault("input", {})
    input_table["formats"] = ["csv", "xlsx"]
    if workflow != "reconcile":
        return document
    datasets = document.setdefault("datasets", {})
    for role in list(datasets):
        if names.get(role):
            datasets[role]["files"] = list(names[role])
        elif role in ("orders", "payments"):
            datasets[role]["files"] = [f"{role}-not-added.csv"]
        else:
            del datasets[role]
    return document


def copy_document(document):
    """
    deep-copies a profile document
    :param document: dictionary
    :returns: copy
    """
    if isinstance(document, dict):
        return {key: copy_document(value) for key, value in document.items()}
    if isinstance(document, list):
        return [copy_document(value) for value in document]
    return document


# suggestions


def column_type(hint):
    """
    suggests a column type from what the file holds
    :param hint: column hints from preview.describe
    :returns: type name
    """
    basic = hint.get("basic_type")
    if basic in ("date", "date text"):
        return "date"
    if basic == "amount":
        return "decimal"
    if basic == "number":
        return "decimal" if hint.get("max_decimals") else "integer"
    if basic == "identifier":
        return "identifier"
    if basic == "digits":
        if hint.get("leading_zeros") or hint.get("id_like_header"):
            return "identifier"
        if KEY_HEADER.search(hint["name"]):
            return "identifier"
        return "integer"
    return "text"


def date_formats(hints):
    """
    suggests date formats from date columns, leaving day/month questions open
    :param hints: column hints of the date columns
    :returns: tuple of formats and open day/month questions
    """
    formats, questions = [], []
    for hint in hints:
        ambiguous = {
            name for pair in hint.get("ambiguous_date_formats", []) for name in pair
        }
        for name in hint.get("date_formats", []):
            if name not in ambiguous and name not in formats:
                formats.append(name)
        for pair in hint.get("ambiguous_date_formats", []):
            if pair not in questions:
                questions.append(pair)
        if hint.get("date_cells") and not hint.get("date_formats") and not formats:
            formats.append("%Y-%m-%d")
    return formats, questions


def amount_rules(hints, currency="USD"):
    """
    suggests how money is written, from amount-like columns
    :param hints: column hints of the money columns
    :param currency: currency code
    :returns: amounts table
    """
    styles = {style for hint in hints for style in hint.get("amount_styles", [])}
    european = styles == {"1.234,56"}
    symbols = sorted({s for hint in hints for s in hint.get("currency_symbols", [])})
    negatives = sorted({s for hint in hints for s in hint.get("negative_styles", [])})
    return {
        "currency": currency,
        "currency_symbols": symbols,
        "thousands_separator": "." if european else ",",
        "decimal_separator": "," if european else ".",
        "max_decimal_places": 2,
        "negative_styles": negatives or ["minus"],
    }


def suggest_table(workflow, description, name_hint):
    """
    drafts clean/compare settings from the first file's columns
    :param workflow: clean or compare
    :param description: preview.describe result for the first file
    :param name_hint: text the profile name is built from
    :returns: tuple of document and open questions
    """
    taken = set()
    columns = {}
    hints = {}
    for hint in description["columns"]:
        name = column_name(hint["name"], taken)
        kind = column_type(hint)
        columns[name] = {"source": hint["name"], "type": kind, "required": False}
        if kind == "text" and hint.get("basic_type") == "text":
            columns[name]["whitespace"] = "trim"
        hints[name] = hint

    key = []
    candidates = [
        name
        for name, column in columns.items()
        if column["type"] in ("identifier", "text")
        and (KEY_HEADER.search(column["source"]) or hints[name].get("id_like_header"))
        and hints[name].get("blank", 1) == 0
    ]
    if candidates:
        key = [candidates[0]]
        columns[key[0]]["type"] = "identifier"
        columns[key[0]]["required"] = True

    blank_tokens = sorted(
        {
            token
            for hint in description["columns"]
            for token in hint.get("blank_tokens", [])
        }
    )
    formats, questions = date_formats(
        [hints[n] for n, c in columns.items() if c["type"] == "date"]
    )
    document = {
        "schema_version": 1,
        "workflow": {"kind": workflow},
        "profile": {
            "name": slug(f"{name_hint}-{workflow}"),
            "version": "1.0.0",
            "description": "Saved from the Ordixa workspace",
        },
        "input": {"formats": ["csv", "xlsx"], "csv_encoding": "utf-8-sig"},
        "table": {"key": key, "blank_values": blank_tokens, "date_formats": formats},
        "columns": columns,
    }
    money = [hints[n] for n, c in columns.items() if c["type"] == "decimal"]
    if money:
        document["amounts"] = amount_rules(money)
    if workflow == "compare":
        document["compare"] = {"ignore": []}
    return document, questions


def pick_header(header, spellings, taken):
    """
    finds the first unused header matching one of several spellings
    :param header: header names
    :param spellings: candidate spellings, best first
    :param taken: headers already used
    :returns: header name or none
    """
    folded = {fold(name): name for name in header if name not in taken}
    for spelling in spellings:
        if spelling in folded:
            return folded[spelling]
    return None


def suggest_reconcile(descriptions, values, name_hint):
    """
    drafts reconciliation settings from the first file of each dataset
    :param descriptions: preview.describe results by role
    :param values: distinct values of the suggested type and status columns
    :param name_hint: text the profile name is built from
    :returns: tuple of document and open questions
    """
    datasets = {}
    questions = []
    money = []
    for role in ("customers", "orders", "payments"):
        description = descriptions.get(role)
        if description is None or not description["header"]:
            continue
        header = description["header"]
        hints = {column["name"]: column for column in description["columns"]}
        taken = set()
        columns = {}
        for field, spellings in FIELD_HEADERS[role].items():
            chosen = pick_header(header, spellings, taken)
            if field == "name" and chosen is None:
                first = pick_header(header, ["first name", "given name"], taken)
                last = pick_header(
                    header, ["last name", "surname", "family name"], taken
                )
                if first and last:
                    columns["name"] = [first, last]
                    taken |= {first, last}
                continue
            if chosen:
                columns[field] = chosen
                taken.add(chosen)
        dataset = {"columns": columns}
        if role == "customers":
            dataset["required"] = False
        date_columns = [
            hints[columns[f]] for f in ("date", "payment_date") if columns.get(f)
        ]
        formats, open_questions = date_formats(date_columns)
        if date_columns:
            dataset["date_formats"] = formats
        questions += [pair for pair in open_questions if pair not in questions]
        money += [hints[columns[f]] for f in ("total", "amount") if columns.get(f)]
        if role == "payments":
            amount = hints.get(columns.get("amount"))
            if amount and amount.get("negative_styles"):
                dataset["refund_amounts"] = "negative"
            maps, excluded = {}, []
            for field, words in (
                ("transaction_type", TRANSACTION_WORDS),
                ("status", STATUS_WORDS),
            ):
                mapping = {}
                for item in values.get(field, []):
                    folded = fold(item["value"])
                    if folded in words:
                        mapping[item["value"]] = words[folded]
                    elif field == "status" and folded in EXCLUDED_STATUS_WORDS:
                        excluded.append(item["value"])
                if mapping:
                    maps[field] = mapping
            if maps:
                dataset["value_maps"] = maps
            if excluded:
                dataset["excluded_statuses"] = excluded
        datasets[role] = dataset

    document = {
        "schema_version": 1,
        "profile": {
            "name": slug(f"{name_hint}-reconcile"),
            "version": "1.0.0",
            "description": "Saved from the Ordixa workspace",
        },
        "input": {"formats": ["csv", "xlsx"], "csv_encoding": "utf-8-sig"},
        "amounts": amount_rules(money),
        "reconciliation": {"missing_customer": "warning"},
        # exact text matching keeps letters, separators, and leading zeros
        "identifiers": {
            "customer_id": "text",
            "order_id": "text",
            "payment_id": "text",
        },
        "datasets": datasets,
    }
    return document, questions


def sample_document(sample_id):
    """
    reads a fictional sample's profile
    :param sample_id: key of SAMPLES
    :returns: document
    """
    return parse_toml(SAMPLES[sample_id]["profile"].read_text("utf-8"))
