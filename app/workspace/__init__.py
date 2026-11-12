"""
Ordixa local workspace: a browser interface for clean, compare, and reconcile

the workspace is a thin local layer over the tested Ordixa engine. It stores
files a person chooses in a git-ignored folder on this computer, turns the
choices made on screen into an ordinary versioned TOML profile, previews it
with the engine's read-only preflight, and runs the unchanged command line
(python -m app.ordixa clean|compare|run) in a child process so every run
directory, manifest, checksum, and exit code is exactly what the CLI writes.

nothing here parses amounts, matches records, or writes results itself
"""
