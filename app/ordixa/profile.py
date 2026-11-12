import difflib
import hashlib
import re
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 1
SUPPORTED_FORMATS = {"csv", "xlsx"}
NEGATIVE_STYLES = {"minus", "parentheses"}
DECIMAL_SEPARATORS = {".", ","}
THOUSANDS_SEPARATORS = {",", ".", " ", "'", ""}
SEVERITIES = {"warning", "error"}
REFUND_SIGNS = {"positive", "negative"}

CANONICAL_STATUSES = {"paid", "partial", "pending", "refunded"}
CANONICAL_TRANSACTION_TYPES = {"payment", "refund", "adjustment"}

ID_FIELDS = {"customer_id", "order_id", "payment_id"}
# numeric keeps the Phase 1A rule (digits only, leading zeros dropped); text
# keeps letters, digits, separators, and leading zeros exactly as exported
IDENTIFIER_MODES = {"numeric", "text"}
DEFAULT_IDENTIFIER_MODE = "numeric"
AMOUNT_FIELDS = {"total", "amount"}
DATE_FIELDS = {"date", "payment_date"}

# canonical fields each dataset role understands; required fields must be mapped
DATASET_FIELDS = {
    "customers": {
        "required": {"customer_id"},
        "optional": {"name", "email", "phone"},
    },
    "orders": {
        "required": {"order_id", "date", "total"},
        "optional": {"customer_id", "currency"},
    },
    "payments": {
        "required": {"payment_id", "order_id", "amount", "transaction_type", "status"},
        "optional": {"payment_date", "currency"},
    },
}
REQUIRED_DATASETS = {"orders", "payments"}
VALUE_MAP_TARGETS = {
    "status": CANONICAL_STATUSES,
    "transaction_type": CANONICAL_TRANSACTION_TYPES,
}

TOP_LEVEL_KEYS = {
    "schema_version",
    "profile",
    "input",
    "amounts",
    "reconciliation",
    "identifiers",
    "datasets",
}
PROFILE_KEYS = {"name", "version", "description"}
INPUT_KEYS = {"formats", "csv_encoding"}
AMOUNT_KEYS = {
    "currency",
    "currency_symbols",
    "thousands_separator",
    "decimal_separator",
    "max_decimal_places",
    "negative_styles",
}
RECONCILIATION_KEYS = {"missing_customer"}
DATASET_KEYS = {
    "required",
    "files",
    "sheet",
    "columns",
    "optional_columns",
    "date_formats",
    "id_prefixes",
    "value_maps",
    "excluded_statuses",
    "refund_amounts",
}

PROFILE_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
DATE_PROBE = datetime(2031, 11, 23, 14, 5, 9)


class ProfileError(ValueError):
    """error raised when an import profile cannot be loaded or is invalid"""

    def __init__(self, problems, source=None):
        self.problems = list(problems)
        self.source = source
        header = f"invalid profile {source}" if source else "invalid profile"
        details = "\n".join(f"  - {problem}" for problem in self.problems)
        super().__init__(f"{header}:\n{details}")


@dataclass(frozen=True)
class AmountRules:
    """monetary parsing rules declared by a profile"""

    currency: str
    currency_symbols: tuple[str, ...] = ()
    thousands_separator: str = ","
    decimal_separator: str = "."
    max_decimal_places: int = 2
    negative_styles: frozenset[str] = frozenset({"minus"})


@dataclass(frozen=True)
class DatasetSpec:
    """profile configuration for one input dataset role"""

    role: str
    required: bool
    files: tuple[str, ...]
    columns: dict[str, tuple[str, ...]]
    sheet: str | None = None
    optional_columns: frozenset[str] = frozenset()
    date_formats: tuple[str, ...] = ()
    id_prefixes: dict[str, str] = field(default_factory=dict)
    value_maps: dict[str, dict[str, str]] = field(default_factory=dict)
    excluded_statuses: frozenset[str] = frozenset()
    refund_amounts: str = "positive"

    def source_columns(self):
        """
        lists every source column referenced by this dataset mapping
        :returns: ordered tuple of source column names
        """
        names = []
        for sources in self.columns.values():
            names.extend(source for source in sources if source not in names)
        return tuple(names)


@dataclass(frozen=True)
class Profile:
    """validated, versioned customer import profile"""

    name: str
    version: str
    description: str
    formats: frozenset[str]
    csv_encoding: str
    amounts: AmountRules
    missing_customer: str
    datasets: dict[str, DatasetSpec]
    source_path: str
    sha256: str
    identifiers: dict[str, str] = field(
        default_factory=lambda: dict.fromkeys(ID_FIELDS, DEFAULT_IDENTIFIER_MODE)
    )

    def identifier_mode(self, canonical):
        """
        names how an identifier family is interpreted
        :param canonical: customer_id, order_id, or payment_id
        :returns: numeric or text
        """
        return self.identifiers.get(canonical, DEFAULT_IDENTIFIER_MODE)


def fold(value):
    """
    folds a source label for case and whitespace insensitive comparison
    :param value: source label
    :returns: folded label
    """
    return " ".join(str(value).split()).casefold()


def unknown_keys(table, allowed, path, problems):
    """
    records keys that the profile schema does not support
    :param table: TOML table to check
    :param allowed: supported keys
    :param path: dotted path used in messages
    :param problems: list collecting validation problems
    :returns: none
    """
    for key in table:
        if key not in allowed:
            hint = difflib.get_close_matches(key, sorted(allowed), n=1)
            suffix = f" (did you mean '{hint[0]}'?)" if hint else ""
            problems.append(f"{path}{key}: unsupported key{suffix}")


def expect_table(container, key, path, problems, required=True):
    """
    reads a nested table from a TOML document
    :param container: parent table
    :param key: table key
    :param path: dotted path used in messages
    :param problems: list collecting validation problems
    :param required: whether a missing table is a problem
    :returns: table dictionary, empty when missing or invalid
    """
    if key not in container:
        if required:
            problems.append(f"{path}{key}: required table is missing")
        return {}

    value = container[key]
    if not isinstance(value, dict):
        problems.append(f"{path}{key}: must be a table")
        return {}

    return value


def expect_string(table, key, path, problems, default=None, required=True):
    """
    reads a non-empty string value
    :param table: TOML table
    :param key: value key
    :param path: dotted path used in messages
    :param problems: list collecting validation problems
    :param default: value returned when the key is optional and missing
    :param required: whether a missing key is a problem
    :returns: string value or default
    """
    if key not in table:
        if required:
            problems.append(f"{path}{key}: required value is missing")
        return default

    value = table[key]
    if not isinstance(value, str) or (required and not value.strip()):
        problems.append(f"{path}{key}: must be a non-empty string")
        return default

    return value


def expect_string_list(table, key, path, problems, default=(), required=False):
    """
    reads a list of non-empty strings
    :param table: TOML table
    :param key: value key
    :param path: dotted path used in messages
    :param problems: list collecting validation problems
    :param default: value returned when the key is optional and missing
    :param required: whether a missing or empty list is a problem
    :returns: tuple of strings
    """
    if key not in table:
        if required:
            problems.append(f"{path}{key}: required list is missing")
        return tuple(default)

    value = table[key]
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        problems.append(f"{path}{key}: must be a list of non-empty strings")
        return tuple(default)

    if required and not value:
        problems.append(f"{path}{key}: must not be empty")

    return tuple(value)


def validate_date_format(date_format, path, problems):
    """
    checks that a strptime format is explicit and round-trips
    :param date_format: strptime-style format string
    :param path: dotted path used in messages
    :param problems: list collecting validation problems
    :returns: none
    """
    if "%Y" not in date_format:
        problems.append(
            f"{path}: '{date_format}' must contain a four-digit year (%Y); "
            "two-digit years are ambiguous"
        )
        return

    has_month = any(token in date_format for token in ("%m", "%b", "%B"))
    if "%d" not in date_format or not has_month:
        problems.append(f"{path}: '{date_format}' must contain a day and a month")
        return

    try:
        # some platforms reject unknown directives in strftime as well
        probe = DATE_PROBE.strftime(date_format.replace("%z", "+0000"))
        parsed = datetime.strptime(probe, date_format.replace("%z", "+0000"))
    except ValueError:
        problems.append(f"{path}: '{date_format}' is not a valid date format")
        return

    if parsed.date() != DATE_PROBE.date():
        problems.append(f"{path}: '{date_format}' does not round-trip a date")


def parse_amount_rules(document, problems):
    """
    validates the amount normalization section
    :param document: parsed TOML document
    :param problems: list collecting validation problems
    :returns: amount rules
    """
    table = expect_table(document, "amounts", "", problems)
    unknown_keys(table, AMOUNT_KEYS, "amounts.", problems)

    currency = expect_string(table, "currency", "amounts.", problems, default="")
    if currency and not CURRENCY_PATTERN.match(currency):
        problems.append("amounts.currency: must be a three-letter ISO code like USD")

    symbols = expect_string_list(table, "currency_symbols", "amounts.", problems)
    thousands = table.get("thousands_separator", ",")
    decimal = table.get("decimal_separator", ".")
    places = table.get("max_decimal_places", 2)
    styles = expect_string_list(
        table, "negative_styles", "amounts.", problems, default=("minus",)
    )

    if thousands not in THOUSANDS_SEPARATORS:
        problems.append(
            "amounts.thousands_separator: must be one of "
            f"{sorted(THOUSANDS_SEPARATORS)}"
        )
    if decimal not in DECIMAL_SEPARATORS:
        problems.append(
            f"amounts.decimal_separator: must be one of {sorted(DECIMAL_SEPARATORS)}"
        )
    if thousands == decimal:
        problems.append(
            "amounts: thousands_separator and decimal_separator must differ"
        )
    if isinstance(places, bool) or not isinstance(places, int) or not 0 <= places <= 2:
        problems.append(
            "amounts.max_decimal_places: must be an integer from 0 to 2; "
            "reconciliation is performed at cent precision"
        )
        places = 2
    for symbol in symbols:
        if any(character in symbol for character in "0123456789-()+.,' "):
            problems.append(
                f"amounts.currency_symbols: '{symbol}' may not contain digits, "
                "signs, parentheses, separators, or spaces"
            )
    for style in styles:
        if style not in NEGATIVE_STYLES:
            problems.append(
                f"amounts.negative_styles: '{style}' is not one of "
                f"{sorted(NEGATIVE_STYLES)}"
            )

    return AmountRules(
        currency=currency or "",
        currency_symbols=tuple(sorted(symbols, key=len, reverse=True)),
        thousands_separator=thousands if isinstance(thousands, str) else ",",
        decimal_separator=decimal if isinstance(decimal, str) else ".",
        max_decimal_places=places,
        negative_styles=frozenset(styles),
    )


def parse_columns(table, role, path, problems):
    """
    validates the canonical-to-source column mapping for a dataset
    :param table: dataset table
    :param role: dataset role name
    :param path: dotted path used in messages
    :param problems: list collecting validation problems
    :returns: mapping of canonical field to source column tuple
    """
    columns_table = expect_table(table, "columns", path, problems)
    fields = DATASET_FIELDS[role]
    allowed = fields["required"] | fields["optional"]
    unknown_keys(columns_table, allowed, f"{path}columns.", problems)

    columns = {}
    seen_sources = {}
    for canonical, source in columns_table.items():
        if canonical not in allowed:
            continue

        if isinstance(source, str) and source.strip():
            sources = (source,)
        elif (
            canonical == "name"
            and isinstance(source, list)
            and source
            and all(isinstance(item, str) and item.strip() for item in source)
        ):
            sources = tuple(source)
        else:
            expected = (
                "a column name or list of column names"
                if canonical == "name"
                else "a column name"
            )
            problems.append(f"{path}columns.{canonical}: must be {expected}")
            continue

        for name in sources:
            if name in seen_sources:
                problems.append(
                    f"{path}columns.{canonical}: source column '{name}' is already "
                    f"mapped to '{seen_sources[name]}'"
                )
            seen_sources[name] = canonical
        columns[canonical] = sources

    for canonical in sorted(fields["required"] - set(columns)):
        problems.append(
            f"{path}columns.{canonical}: required field has no source column mapping"
        )

    return columns


def parse_value_maps(table, columns, path, problems):
    """
    validates explicit source-to-canonical value mappings
    :param table: dataset table
    :param columns: validated column mapping for the dataset
    :param path: dotted path used in messages
    :param problems: list collecting validation problems
    :returns: mapping of canonical field to folded source value mapping
    """
    maps_table = expect_table(table, "value_maps", path, problems, required=False)
    allowed = {name for name in VALUE_MAP_TARGETS if name in columns}
    unknown_keys(maps_table, allowed, f"{path}value_maps.", problems)

    value_maps = {}
    for canonical, mapping in maps_table.items():
        if canonical not in allowed:
            continue
        if not isinstance(mapping, dict) or not mapping:
            problems.append(f"{path}value_maps.{canonical}: must be a non-empty table")
            continue

        targets = VALUE_MAP_TARGETS[canonical]
        folded = {}
        for source_value, target in mapping.items():
            label = f'{path}value_maps.{canonical}."{source_value}"'
            if not str(source_value).strip():
                problems.append(f"{label}: source value must not be blank")
                continue
            if target not in targets:
                problems.append(
                    f"{label}: {target!r} is not a supported {canonical}; "
                    f"use one of {sorted(targets)}"
                )
                continue
            key = fold(source_value)
            if key in folded and folded[key] != target:
                problems.append(f"{label}: conflicts with another mapping")
            folded[key] = target
        value_maps[canonical] = folded

    return value_maps


def parse_identifier_modes(document, problems):
    """
    validates the optional identifier interpretation section

    a mode applies to an identifier family everywhere it appears, so an order
    id is read the same way in the orders file and in payment references
    :param document: parsed TOML document
    :param problems: list collecting validation problems
    :returns: mapping of identifier field to mode
    """
    table = expect_table(document, "identifiers", "", problems, required=False)
    unknown_keys(table, ID_FIELDS, "identifiers.", problems)

    modes = dict.fromkeys(ID_FIELDS, DEFAULT_IDENTIFIER_MODE)
    for canonical, mode in table.items():
        if canonical not in ID_FIELDS:
            continue
        if mode not in IDENTIFIER_MODES:
            problems.append(
                f"identifiers.{canonical}: must be one of {sorted(IDENTIFIER_MODES)}"
            )
            continue
        modes[canonical] = mode
    return modes


def parse_dataset(role, table, accepted_formats, problems):
    """
    validates one dataset role configuration
    :param role: dataset role name
    :param table: dataset table
    :param accepted_formats: input formats accepted by the profile
    :param problems: list collecting validation problems
    :returns: dataset specification
    """
    path = f"datasets.{role}."
    unknown_keys(table, DATASET_KEYS, path, problems)

    required = table.get("required", role in REQUIRED_DATASETS)
    if not isinstance(required, bool):
        problems.append(f"{path}required: must be true or false")
        required = True
    if role in REQUIRED_DATASETS and not required:
        problems.append(f"{path}required: the {role} dataset cannot be optional")

    files = expect_string_list(table, "files", path, problems, required=True)
    for pattern in files:
        if "/" in pattern or "\\" in pattern or ".." in pattern:
            problems.append(
                f"{path}files: {pattern!r} must be a file name pattern without "
                "directories"
            )
            continue
        extension = pattern.rsplit(".", 1)[-1].lower() if "." in pattern else ""
        if extension not in accepted_formats:
            problems.append(
                f"{path}files: {pattern!r} must end in an accepted format "
                f"extension {sorted(accepted_formats)}"
            )

    sheet = expect_string(table, "sheet", path, problems, required=False)
    columns = parse_columns(table, role, path, problems)

    optional_columns = expect_string_list(table, "optional_columns", path, problems)
    required_sources = {
        source
        for canonical in DATASET_FIELDS[role]["required"]
        for source in columns.get(canonical, ())
    }
    mapped_sources = {source for sources in columns.values() for source in sources}
    for source in optional_columns:
        if source not in mapped_sources:
            problems.append(
                f"{path}optional_columns: {source!r} is not a mapped source column"
            )
        elif source in required_sources:
            problems.append(
                f"{path}optional_columns: {source!r} feeds a required field and "
                "cannot be optional"
            )

    date_formats = expect_string_list(table, "date_formats", path, problems)
    if DATE_FIELDS & set(columns) and not date_formats:
        problems.append(
            f"{path}date_formats: required because a date column is mapped; "
            "dates are never guessed"
        )
    for date_format in date_formats:
        validate_date_format(date_format, f"{path}date_formats", problems)

    prefixes_table = expect_table(table, "id_prefixes", path, problems, required=False)
    prefix_fields = ID_FIELDS & set(columns)
    unknown_keys(prefixes_table, prefix_fields, f"{path}id_prefixes.", problems)
    id_prefixes = {}
    for canonical, prefix in prefixes_table.items():
        if canonical not in prefix_fields:
            continue
        if not isinstance(prefix, str) or not prefix or prefix.strip() != prefix:
            problems.append(
                f"{path}id_prefixes.{canonical}: must be a non-empty string without "
                "surrounding spaces"
            )
            continue
        id_prefixes[canonical] = prefix

    value_maps = parse_value_maps(table, columns, path, problems)

    excluded = expect_string_list(table, "excluded_statuses", path, problems)
    if excluded and "status" not in columns:
        problems.append(f"{path}excluded_statuses: requires a mapped status column")
    excluded_folded = frozenset(fold(value) for value in excluded)
    for value in sorted(excluded_folded & set(value_maps.get("status", {}))):
        problems.append(
            f"{path}excluded_statuses: {value!r} is also mapped in value_maps.status"
        )

    refund_amounts = table.get("refund_amounts", "positive")
    if role != "payments" and "refund_amounts" in table:
        problems.append(f"{path}refund_amounts: only applies to the payments dataset")
    elif refund_amounts not in REFUND_SIGNS:
        problems.append(f"{path}refund_amounts: must be one of {sorted(REFUND_SIGNS)}")
        refund_amounts = "positive"

    return DatasetSpec(
        role=role,
        required=required,
        files=files,
        sheet=sheet,
        columns=columns,
        optional_columns=frozenset(optional_columns),
        date_formats=date_formats,
        id_prefixes=id_prefixes,
        value_maps=value_maps,
        excluded_statuses=excluded_folded,
        refund_amounts=refund_amounts,
    )


def validate_profile_document(document, source_path="<memory>", sha256=""):
    """
    validates a parsed profile document and builds a profile
    :param document: dictionary parsed from TOML
    :param source_path: profile location used in messages
    :param sha256: checksum of the profile file
    :returns: validated profile
    """
    if "workflow" in document:
        raise ProfileError(
            [
                "workflow: this is a spreadsheet clean/compare profile; use "
                "python -m app.ordixa clean or compare instead of run"
            ],
            source_path,
        )

    problems = []
    unknown_keys(document, TOP_LEVEL_KEYS, "", problems)

    schema_version = document.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        problems.append(
            f"schema_version: must be {SCHEMA_VERSION} (found {schema_version!r})"
        )

    meta = expect_table(document, "profile", "", problems)
    unknown_keys(meta, PROFILE_KEYS, "profile.", problems)
    name = expect_string(meta, "name", "profile.", problems, default="")
    version = expect_string(meta, "version", "profile.", problems, default="")
    description = expect_string(
        meta, "description", "profile.", problems, default="", required=False
    )
    if name and not PROFILE_NAME_PATTERN.match(name):
        problems.append(
            "profile.name: use lowercase letters, digits, and hyphens (max 64)"
        )
    if version and not VERSION_PATTERN.match(version):
        problems.append("profile.version: must look like MAJOR.MINOR.PATCH")

    input_table = expect_table(document, "input", "", problems, required=False)
    unknown_keys(input_table, INPUT_KEYS, "input.", problems)
    formats = expect_string_list(
        input_table, "formats", "input.", problems, default=("csv",)
    )
    for input_format in formats:
        if input_format not in SUPPORTED_FORMATS:
            problems.append(
                f"input.formats: {input_format!r} is not supported; "
                f"use {sorted(SUPPORTED_FORMATS)}"
            )
    accepted_formats = set(formats) & SUPPORTED_FORMATS
    csv_encoding = expect_string(
        input_table,
        "csv_encoding",
        "input.",
        problems,
        default="utf-8-sig",
        required=False,
    )
    try:
        "".encode(csv_encoding)
    except LookupError:
        problems.append(f"input.csv_encoding: unknown encoding {csv_encoding!r}")

    amounts = parse_amount_rules(document, problems)

    reconciliation = expect_table(
        document, "reconciliation", "", problems, required=False
    )
    unknown_keys(reconciliation, RECONCILIATION_KEYS, "reconciliation.", problems)
    missing_customer = reconciliation.get("missing_customer", "warning")
    if missing_customer not in SEVERITIES:
        problems.append(
            f"reconciliation.missing_customer: must be one of {sorted(SEVERITIES)}"
        )
        missing_customer = "warning"

    identifiers = parse_identifier_modes(document, problems)

    datasets_table = expect_table(document, "datasets", "", problems)
    unknown_keys(datasets_table, set(DATASET_FIELDS), "datasets.", problems)
    datasets = {}
    for role in DATASET_FIELDS:
        if role not in datasets_table:
            if role in REQUIRED_DATASETS:
                problems.append(f"datasets.{role}: required dataset is not configured")
            continue
        table = datasets_table[role]
        if not isinstance(table, dict):
            problems.append(f"datasets.{role}: must be a table")
            continue
        datasets[role] = parse_dataset(role, table, accepted_formats, problems)

    if problems:
        raise ProfileError(problems, source_path)

    return Profile(
        name=name,
        version=version,
        description=description,
        formats=frozenset(formats),
        csv_encoding=csv_encoding,
        amounts=amounts,
        missing_customer=missing_customer,
        datasets=datasets,
        source_path=str(source_path),
        sha256=sha256,
        identifiers=identifiers,
    )


def read_profile_document(path):
    """
    reads and parses a TOML profile file without validating it
    :param path: profile file path
    :returns: tuple of parsed document and SHA-256 of the file bytes
    """
    path = Path(path)

    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ProfileError([f"cannot read profile: {error.strerror}"], path) from error

    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as error:
        raise ProfileError(["profile must be UTF-8 encoded"], path) from error
    except tomllib.TOMLDecodeError as error:
        raise ProfileError([f"TOML syntax error: {error}"], path) from error

    return document, hashlib.sha256(raw).hexdigest()


def load_profile(path):
    """
    loads and validates a TOML import profile
    :param path: profile file path
    :returns: validated profile
    """
    document, sha256 = read_profile_document(path)
    return validate_profile_document(document, source_path=Path(path), sha256=sha256)
