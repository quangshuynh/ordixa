"""
writes Ordixa profile documents as TOML

profiles hold only strings, booleans, integers, lists of strings, and nested
tables, so this small writer covers exactly that. Every document it writes is
read back with tomllib and must equal the original, so a profile saved from
the workspace means precisely what the screen showed
"""

import re
import tomllib

BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


class TomlWriteError(ValueError):
    """raised for a value a profile cannot hold"""


def string(text):
    """
    quotes text as a TOML basic string
    :param text: python string
    :returns: TOML text
    """
    out = []
    for character in text:
        if character in ESCAPES:
            out.append(ESCAPES[character])
        elif ord(character) < 0x20 or ord(character) == 0x7F:
            out.append(f"\\u{ord(character):04X}")
        else:
            out.append(character)
    return '"' + "".join(out) + '"'


def key(name):
    """
    writes a table or value key, quoting it when it is not a bare key
    :param name: key text
    :returns: TOML key
    """
    if not isinstance(name, str) or not name:
        raise TomlWriteError(f"keys must be non-empty text, not {name!r}")
    return name if BARE_KEY.match(name) else string(name)


def value(item):
    """
    writes a scalar or list value
    :param item: python value
    :returns: TOML text
    """
    if isinstance(item, bool):
        return "true" if item else "false"
    if isinstance(item, int):
        return str(item)
    if isinstance(item, str):
        return string(item)
    if isinstance(item, (list, tuple)):
        return "[" + ", ".join(value(element) for element in item) + "]"
    raise TomlWriteError(f"unsupported profile value {type(item).__name__}")


def write_table(lines, path, table):
    """
    appends one table and its sub-tables
    :param lines: output lines
    :param path: list of keys leading to this table
    :param table: dictionary
    :returns: none
    """
    scalars = [(k, v) for k, v in table.items() if not isinstance(v, dict)]
    tables = [(k, v) for k, v in table.items() if isinstance(v, dict)]
    if path and (scalars or not tables):
        if lines:
            lines.append("")
        lines.append("[" + ".".join(key(part) for part in path) + "]")
    for name, item in scalars:
        if item is None:
            continue
        lines.append(f"{key(name)} = {value(item)}")
    for name, item in tables:
        write_table(lines, [*path, name], item)


def dumps(document, header=None):
    """
    serializes a profile document and proves it reads back unchanged
    :param document: dictionary
    :param header: optional comment lines placed at the top
    :returns: TOML text
    """
    lines = [f"# {line}".rstrip() for line in (header or [])]
    body = []
    write_table(body, [], document)
    text = "\n".join(lines + ([""] if lines else []) + body) + "\n"
    if tomllib.loads(text) != strip_none(document):
        raise TomlWriteError("the profile did not read back unchanged")
    return text


def strip_none(document):
    """
    drops unset values, which TOML cannot represent
    :param document: dictionary
    :returns: copy without none values
    """
    return {
        k: strip_none(v) if isinstance(v, dict) else v
        for k, v in document.items()
        if v is not None
    }
