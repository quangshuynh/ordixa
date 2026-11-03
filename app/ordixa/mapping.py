from dataclasses import dataclass, field

from app.ordixa.adapters import malformed_message
from app.ordixa.issues import ERROR, EXCLUDED, WARNING, Issue
from app.ordixa.normalize import (
    NormalizationError,
    display_value,
    identifier_spelling,
    is_blank,
    parse_amount,
    parse_currency,
    parse_date,
    parse_identifier,
)
from app.ordixa.profile import VALUE_MAP_TARGETS, fold
from app.validators.validate import is_valid_email, normalize_email, normalize_phone

PRIMARY_KEYS = {
    "customers": "customer_id",
    "orders": "order_id",
    "payments": "payment_id",
}


@dataclass
class Record:
    """one source row translated into canonical fields"""

    dataset: str
    file: str
    row: int
    ref: str
    original_values: str
    fields: dict = field(default_factory=dict)
    issues: list = field(default_factory=list)
    # primary identifier as spelled in the export, used to detect two different
    # spellings that normalize to the same canonical identifier
    spelling: str = ""

    @property
    def rejected(self):
        """
        reports whether any blocking issue applies to the row
        :returns: true when the row cannot be used
        """
        return any(issue.severity == ERROR for issue in self.issues)

    @property
    def excluded(self):
        """
        reports whether the profile excluded the row from totals
        :returns: true when the row was excluded on purpose
        """
        return not self.rejected and any(
            issue.severity == EXCLUDED for issue in self.issues
        )

    @property
    def accepted(self):
        """
        reports whether the row continues into reconciliation
        :returns: true when the row is neither rejected nor excluded
        """
        return not self.rejected and not self.excluded

    def add_issue(self, severity, code, message, field_name="", value=""):
        """
        attaches a data-quality issue to the record
        :param severity: error, warning, or excluded
        :param code: stable machine-readable issue code
        :param message: human-readable explanation
        :param field_name: canonical field involved
        :param value: original value involved
        :returns: none
        """
        self.issues.append(
            Issue(
                dataset=self.dataset,
                file=self.file,
                row=self.row,
                severity=severity,
                code=code,
                message=message,
                record_id=self.ref,
                field=field_name,
                value=value,
                original_values=self.original_values,
            )
        )


def raw_value(values, spec, canonical):
    """
    reads the raw source value for a canonical field
    :param values: source row values keyed by header
    :param spec: dataset specification
    :param canonical: canonical field name
    :returns: raw value, or none when the field is unmapped or absent
    """
    sources = spec.columns.get(canonical)
    if not sources:
        return None
    if len(sources) == 1:
        return values.get(sources[0])
    parts = [display_value(values.get(source)) for source in sources]
    return " ".join(part for part in parts if part)


def map_value(record, spec, canonical, value):
    """
    translates a status or transaction type through the profile's explicit map
    :param record: record being built
    :param spec: dataset specification
    :param canonical: canonical field name
    :param value: raw source value
    :returns: canonical value or none when it cannot be mapped
    """
    text = display_value(value)
    if not text:
        record.add_issue(
            ERROR, f"missing_{canonical}", f"{canonical} is blank", canonical
        )
        return None

    mapping = spec.value_maps.get(canonical)
    folded = fold(text)
    if mapping is not None:
        mapped = mapping.get(folded)
    else:
        mapped = folded if folded in VALUE_MAP_TARGETS[canonical] else None

    if mapped is None:
        record.add_issue(
            ERROR,
            f"unmapped_{canonical}",
            f"{canonical} {text!r} is not mapped by the profile; unknown values are "
            "never guessed",
            canonical,
            text,
        )
    return mapped


def identifier_parser(spec, profile, canonical):
    """
    builds the parser for one identifier column using the profile's rules
    :param spec: dataset specification
    :param profile: validated import profile
    :param canonical: customer_id, order_id, or payment_id
    :returns: function converting a raw value into a canonical identifier
    """
    prefix = spec.id_prefixes.get(canonical)
    mode = profile.identifier_mode(canonical)
    return lambda value: parse_identifier(value, prefix, mode)


def normalize_field(record, canonical, severity, parser, value):
    """
    runs a normalization function and records failures as issues
    :param record: record being built
    :param canonical: canonical field name
    :param severity: severity used when the value is unusable
    :param parser: function converting the raw value
    :param value: raw source value
    :returns: normalized value or none
    """
    try:
        return parser(value)
    except NormalizationError as error:
        record.add_issue(
            severity,
            error.code,
            f"{canonical}: {error}",
            canonical,
            display_value(value),
        )
        return None


def build_customer(record, spec, profile, values):
    """
    maps customer fields; contact problems are warnings, never rejections
    :param record: record being built
    :param spec: dataset specification
    :param profile: validated import profile
    :param values: source row values
    :returns: none
    """
    fields = record.fields
    fields["customer_id"] = normalize_field(
        record,
        "customer_id",
        ERROR,
        identifier_parser(spec, profile, "customer_id"),
        raw_value(values, spec, "customer_id"),
    )

    if "name" in spec.columns:
        name = display_value(raw_value(values, spec, "name"))
        fields["name"] = name or None
        if not name:
            record.add_issue(WARNING, "missing_name", "customer name is blank", "name")

    if "email" in spec.columns:
        raw_email = raw_value(values, spec, "email")
        email = normalize_email(None if is_blank(raw_email) else raw_email)
        fields["email"] = email
        if email is None:
            record.add_issue(WARNING, "missing_email", "email is blank", "email")
        elif not is_valid_email(email):
            record.add_issue(
                WARNING,
                "invalid_email",
                f"email {email!r} is malformed",
                "email",
                email,
            )

    if "phone" in spec.columns:
        raw_phone = raw_value(values, spec, "phone")
        phone = normalize_phone(None if is_blank(raw_phone) else raw_phone)
        fields["phone"] = phone or (display_value(raw_phone) or None)
        if is_blank(raw_phone):
            record.add_issue(WARNING, "missing_phone", "phone is blank", "phone")
        elif phone is None:
            record.add_issue(
                WARNING,
                "invalid_phone",
                "phone is not a 10-digit number; kept as provided",
                "phone",
                display_value(raw_phone),
            )


def build_currency(record, spec, profile, values):
    """
    validates the optional currency column against the profile currency
    :param record: record being built
    :param spec: dataset specification
    :param profile: validated import profile
    :param values: source row values
    :returns: none
    """
    if "currency" not in spec.columns:
        return

    value = raw_value(values, spec, "currency")
    if is_blank(value):
        record.add_issue(
            WARNING,
            "missing_currency",
            f"currency is blank; treated as the profile currency "
            f"{profile.amounts.currency}",
            "currency",
        )
        return

    normalize_field(
        record,
        "currency",
        ERROR,
        lambda raw: parse_currency(raw, profile.amounts.currency),
        value,
    )


def build_order(record, spec, profile, values):
    """
    maps order fields
    :param record: record being built
    :param spec: dataset specification
    :param profile: validated import profile
    :param values: source row values
    :returns: none
    """
    fields = record.fields
    fields["order_id"] = normalize_field(
        record,
        "order_id",
        ERROR,
        identifier_parser(spec, profile, "order_id"),
        raw_value(values, spec, "order_id"),
    )
    fields["date"] = normalize_field(
        record,
        "date",
        ERROR,
        lambda value: parse_date(value, spec.date_formats),
        raw_value(values, spec, "date"),
    )
    fields["total"] = normalize_field(
        record,
        "total",
        ERROR,
        lambda value: parse_amount(value, profile.amounts),
        raw_value(values, spec, "total"),
    )
    if fields["total"] is not None and fields["total"] <= 0:
        record.add_issue(
            ERROR,
            "non_positive_total",
            "order total must be greater than zero",
            "total",
            str(fields["total"]),
        )

    fields["customer_id"] = None
    if "customer_id" in spec.columns:
        raw_customer = raw_value(values, spec, "customer_id")
        if is_blank(raw_customer):
            record.add_issue(
                profile.missing_customer,
                "missing_customer_reference",
                "order has no customer reference",
                "customer_id",
            )
        else:
            fields["customer_id"] = normalize_field(
                record,
                "customer_id",
                profile.missing_customer,
                identifier_parser(spec, profile, "customer_id"),
                raw_customer,
            )

    build_currency(record, spec, profile, values)


def build_payment(record, spec, profile, values):
    """
    maps payment fields and applies the profile's refund sign convention
    :param record: record being built
    :param spec: dataset specification
    :param profile: validated import profile
    :param values: source row values
    :returns: none
    """
    fields = record.fields
    fields["payment_id"] = normalize_field(
        record,
        "payment_id",
        ERROR,
        identifier_parser(spec, profile, "payment_id"),
        raw_value(values, spec, "payment_id"),
    )
    raw_order = raw_value(values, spec, "order_id")
    fields["order_id"] = normalize_field(
        record,
        "order_id",
        ERROR,
        identifier_parser(spec, profile, "order_id"),
        raw_order,
    )
    fields["order_ref"] = display_value(raw_order)

    fields["payment_date"] = None
    if "payment_date" in spec.columns:
        raw_date = raw_value(values, spec, "payment_date")
        if not is_blank(raw_date):
            fields["payment_date"] = normalize_field(
                record,
                "payment_date",
                WARNING,
                lambda value: parse_date(value, spec.date_formats),
                raw_date,
            )

    raw_status = raw_value(values, spec, "status")
    if fold(display_value(raw_status)) in spec.excluded_statuses:
        fields["status"] = None
        record.add_issue(
            EXCLUDED,
            "excluded_status",
            f"status {display_value(raw_status)!r} is excluded by the profile; the "
            "transaction is not counted",
            "status",
            display_value(raw_status),
        )
    else:
        fields["status"] = map_value(record, spec, "status", raw_status)

    fields["transaction_type"] = map_value(
        record, spec, "transaction_type", raw_value(values, spec, "transaction_type")
    )
    amount = normalize_field(
        record,
        "amount",
        ERROR,
        lambda value: parse_amount(value, profile.amounts),
        raw_value(values, spec, "amount"),
    )

    if amount is not None and fields["transaction_type"] == "refund":
        if spec.refund_amounts == "negative":
            if amount > 0:
                record.add_issue(
                    ERROR,
                    "refund_sign",
                    "refund amount is positive but this profile expects refunds to "
                    "be exported as negative amounts",
                    "amount",
                    str(amount),
                )
                amount = None
            else:
                amount = -amount
        elif amount < 0:
            record.add_issue(
                ERROR,
                "refund_sign",
                "refund amount is negative but this profile expects refunds to be "
                "exported as positive amounts",
                "amount",
                str(amount),
            )
            amount = None

    fields["amount"] = amount
    build_currency(record, spec, profile, values)


BUILDERS = {
    "customers": build_customer,
    "orders": build_order,
    "payments": build_payment,
}


def canonicalize(table, spec, profile):
    """
    translates every row of a source table into canonical records
    :param table: source table read by an adapter
    :param spec: dataset specification
    :param profile: validated import profile
    :returns: list of canonical records in source order
    """
    sources = spec.source_columns()
    records = []

    for row_number in table.malformed_rows:
        record = Record(spec.role, table.path.name, row_number, "", "")
        record.add_issue(
            ERROR,
            "malformed_row",
            malformed_message(
                table,
                row_number,
                "row has a different number of cells than the header",
            ),
        )
        records.append(record)

    for source_row in table.rows:
        values = source_row.values
        original = " | ".join(
            f"{name}={display_value(values.get(name))}"
            for name in sources
            if name in values
        )
        primary = PRIMARY_KEYS[spec.role]
        raw_primary = raw_value(values, spec, primary)
        record = Record(
            dataset=spec.role,
            file=table.path.name,
            row=source_row.row,
            ref=display_value(raw_primary),
            original_values=original,
            spelling=identifier_spelling(raw_primary),
        )
        BUILDERS[spec.role](record, spec, profile, values)
        records.append(record)

    records.sort(key=lambda record: record.row)
    return records
