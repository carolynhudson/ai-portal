"""The model index's SQL surface: what is dynamic, what is bound, and what a hostile
input can actually do.

These tests exist because the same finding has been re-raised against this module
several times — "string-based query construction is an injection vector". It is not,
and the reason is checkable rather than arguable:

  * the only interpolated text is a list of column NAMES;
  * every name passes an allowlist that raises instead of sanitising;
  * every quoted name is wrapped in double quotes, so it is an identifier token;
  * every VALUE is a `?` placeholder, and there is exactly one per column.

Asserting those four facts is the point. If a future edit adds an unquoted name, drops
a placeholder, or turns the guard into a filter that "cleans" hostile input, a test here
fails — which is the difference between a claim and a guard.

Everything runs against a throwaway AI_PORTAL_HOME. Nothing here touches the real
library, systemd, or the network.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("AI_PORTAL_HOME", tempfile.mkdtemp(prefix="aipp-sql-"))
# Set the password here too rather than relying on another module having set it first:
# pytest's file order decides who runs first, and a module that reaches app.main without
# it dies with KeyError: 'AI_PORTAL_PASSWORD' during import — which is how this file once
# broke two tests in test_modelindex.py.
os.environ.setdefault("AI_PORTAL_PASSWORD", "test-password-123")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import db, modelindex as mi, paths  # noqa: E402
import struct  # noqa: E402

from tests.test_modelindex import (  # noqa: E402
    GGUF_STRING,
    _append_kv_key,
    _enc_str,
    _write_fake_gguf,
)

# Names that would break a statement if they were ever embedded as text. Each one is a
# real payload shape, not a synthetic nuisance.
HOSTILE = (
    'a; DROP TABLE model_index;--',
    'a" , "b',
    'a) VALUES (1',
    'a--b',
    '/*x*/',
    '1abc',
    'a b',
    '--',
    'a\tb',
    'a\nb',
    '"quoted"',
)


def _fresh() -> None:
    """A clean index in the throwaway home, so a test cannot see another test's rows.

    `init()` alone does not make the index clean — it creates the table if it is absent
    and leaves existing rows exactly as they were. That is the right behaviour for the
    app and the wrong one for a test: rows() sorts by size, so a leftover row from any
    earlier test in the same home becomes rows()[0] and a test that assumed "my model is
    the only row" reads someone else's data. Empty the library directory and the table.
    """
    import shutil

    db.init()
    mi.init()
    if paths.LLAMA_MODELS.is_dir():
        for child in paths.LLAMA_MODELS.iterdir():
            shutil.rmtree(child, ignore_errors=True)
    # db._conn() is a thread-local shared connection, not a handle this test owns:
    # closing it poisons every later db.init() in the same process with
    # "Cannot operate on a closed database". Delete the rows and leave it open.
    db._conn().execute("DELETE FROM model_index")
    db._conn().commit()


# --- the allowlist is a gate, not a cleaner ------------------------------------

def test_hostile_column_names_raise():
    _fresh()
    for name in HOSTILE:
        try:
            mi._safe_columns((name,))
        except RuntimeError:
            continue
        raise AssertionError(f"hostile column name accepted: {name!r}")


def test_allowlist_rejects_leading_underscore_names():
    # Not an injection question — a convention question. `_rowid`-style names are the
    # ones SQLite treats specially, and this module refuses them outright.
    _fresh()
    try:
        mi._safe_columns(("_rowid",))
        raise AssertionError("leading-underscore name accepted")
    except RuntimeError:
        pass


def test_allowlist_accepts_only_the_real_columns():
    _fresh()
    frag = mi._safe_columns(mi._WRITE_COLUMNS)
    # Quoted, comma-separated, one per column, no doubled quote anywhere.
    assert frag.count('"') == 2 * len(mi._WRITE_COLUMNS)
    assert '""' not in frag
    assert len(frag.split(",")) == len(mi._WRITE_COLUMNS)


# --- values are parameters, never text -----------------------------------------

def test_insert_has_one_placeholder_per_column():
    _fresh()
    sql = mi._insert_sql()
    assert sql.count("?") == len(mi._WRITE_COLUMNS)
    # The VALUES clause is nothing but placeholders: no literal, no concatenation.
    values = sql[sql.index("VALUES"):]
    assert set(values.replace("(", "").replace(")", "").replace("VALUES", "")
               .replace(",", "").replace(" ", "")) == {"?"}


def test_row_values_is_positional_and_refuses_a_missing_column():
    _fresh()
    row = dict.fromkeys(mi._WRITE_COLUMNS, 1)
    vals = mi._row_values(dict(row))
    assert len(vals) == len(mi._WRITE_COLUMNS)
    # A column the row cannot fill must raise, not quietly store NULL: a NULL reads as
    # "the model has no value for that" when the truth is "the writer forgot it".
    broken = dict(row)
    broken.pop("id")
    try:
        mi._row_values(broken)
        raise AssertionError("missing column silently accepted")
    except (RuntimeError, KeyError):
        pass


def test_a_hostile_value_is_stored_as_inert_data():
    """The only attacker-shaped input this module has: a model DIRECTORY name."""
    _fresh()
    evil = 'z"),(1); DROP TABLE model_index;--'
    d = paths.LLAMA_MODELS / evil
    d.mkdir(parents=True, exist_ok=True)
    try:
        _write_fake_gguf(d / "m.gguf", {
            "block_count": 8, "attention.head_count": 4, "attention.head_count_kv": 2,
            "embedding_length": 256, "context_length": 4096,
        })
        mi.rescan()
        conn = sqlite3.connect(str(db.DB))
        try:
            # If the payload had closed a VALUES list and injected a statement, this
            # table would be gone.
            rows = conn.execute("SELECT id FROM model_index").fetchall()
            assert rows, "scan stored nothing"
            stored = [r[0] for r in rows if "DROP TABLE" in r[0]]
            assert stored, "payload was not stored at all"
            # Stored verbatim, as data — not executed, not partially interpreted.
            assert stored[0] == evil
            # And the table still has its full column set.
            cols = {r[1] for r in conn.execute("PRAGMA table_info(model_index)")}
            assert "id" in cols and "est_total" in cols
        finally:
            conn.close()
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)
        _fresh()


def test_quoting_does_not_change_what_the_guard_rejects():
    """Quoting is presentation; the gate still decides.

    If quoting were ever used *instead of* rejection — i.e. hostile text escaped and
    embedded — this test fails, because the name must never reach the fragment.
    """
    _fresh()
    for name in HOSTILE:
        try:
            frag = mi._safe_columns((name,))
        except RuntimeError:
            continue
        raise AssertionError(f"hostile name reached the SQL fragment: {frag!r}")


def test_schema_and_write_columns_agree():
    """A column in _WRITE_COLUMNS with no SCHEMA entry is silently dropped on insert.

    That is the failure mode the `sidecar_type` column nearly caused: the value was
    computed, put in the row, and would have gone nowhere.
    """
    _fresh()
    declared = set()
    for line in mi.SCHEMA.splitlines():
        for token in line.replace("(", " ").replace(")", " ").replace(",", " ").split():
            declared.add(token)
    missing = [c for c in mi._WRITE_COLUMNS if c not in declared]
    assert not missing, f"write columns absent from SCHEMA: {missing}"


def test_new_column_migrates_with_the_type_schema_declares():
    """An upgraded database must get the same type a fresh one gets."""
    _fresh()
    assert mi._declared_type("sidecar_type") == "TEXT"
    conn = sqlite3.connect(str(db.DB))
    try:
        cols = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(model_index)")}
        assert cols.get("sidecar_type") == "TEXT", cols.get("sidecar_type")
    finally:
        conn.close()


# --- the sidecar fact, and the fixture trap that can fake it ------------------

def test_sidecar_type_is_stored_and_survives_a_round_trip():
    """`general.type` is a top-level STRING key.

    The fixture helper `_append_kv_key` writes every value as a GGUF bool
    (`_enc_value(bool(value))`), so using it here stores `1` where a real mmproj
    stores the string `mmproj`. A test built that way would pass while asserting
    nothing about the real reader, and the UI tooltip would print
    "1 declares clip.has_vision_encoder". Appended by hand instead, with the
    string type tag, so the round trip is the one the real library takes.
    """
    import struct

    from tests.test_modelindex import GGUF_BOOL, GGUF_STRING, _enc_str, _write_fake_gguf

    _fresh()
    d = paths.LLAMA_MODELS / "sidecar-typed"
    d.mkdir(parents=True, exist_ok=True)
    try:
        _write_fake_gguf(d / "m.gguf", {
            "block_count": 8, "attention.head_count": 4, "attention.head_count_kv": 2,
            "embedding_length": 256, "context_length": 4096,
        })
        side = d / "mmproj-x.gguf"
        _write_fake_gguf(side, {"block_count": 2})
        # Append two unprefixed top-level keys with their real GGUF types.
        raw = bytearray(side.read_bytes())
        n = struct.unpack_from("<Q", raw, 16)[0]
        struct.pack_into("<Q", raw, 16, n + 2)
        side.write_bytes(
            bytes(raw)
            + _enc_str("general.type") + struct.pack("<I", GGUF_STRING) + _enc_str("mmproj")
            + _enc_str("clip.has_vision_encoder") + struct.pack("<I", GGUF_BOOL)
            + struct.pack("<?", True)
        )
        mi.rescan()
        row = mi.rows()[0]
        assert row["has_mmproj"] == 1
        # The declared type must come back as the string the file wrote, not a coerced
        # 0/1 — that coercion is exactly what the bool-only fixture helper produces.
        assert row["sidecar_type"] == "mmproj", row["sidecar_type"]
        # And vision is derived from the sidecar's own declaration: the main header
        # here declares nothing about vision.
        assert row["has_vision"] == 1
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)
        _fresh()


# --- the ALTER path: a second dynamic statement, guarded the same way ---------

def test_declared_type_can_only_ever_emit_a_bare_type_name():
    """`ALTER TABLE ... ADD COLUMN {name} {type}` interpolates the type too.

    The name is allowlisted; the type comes from _declared_type(). A reader who assumed
    the type is safe because it "comes from SCHEMA" should be able to check it: whatever
    the input, the output is one of the three words SCHEMA uses, or the TEXT fallback.
    A hostile name must not be able to smuggle SQL through the type half of the statement.
    """
    _fresh()
    allowed = {"TEXT", "INTEGER", "REAL"}
    # Only names _safe_columns would let through: a bare identifier. Anything with
    # punctuation in it never reaches the statement at all, so testing those here
    # would prove the wrong thing.
    hostile = [
        "drop_table", "model_index", "select", "x",
        "x_integer_drop_table_model_index",
        "sidecar_type", "template_markers",
    ]
    assert not any(n.isidentifier() is False for n in hostile)
    for name in list(mi._WRITE_COLUMNS) + hostile:
        got = mi._declared_type(name)
        assert got in allowed, f"{name!r} produced type {got!r}"
    # A bare identifier that SCHEMA does not declare gets the TEXT fallback, never a
    # type smuggled in from the name itself.
    assert mi._declared_type("drop_table") == "TEXT"


def test_alter_migration_cannot_be_reached_with_a_hostile_name():
    """The migration loop validates each name before ALTER, so a hostile name must raise
    before any statement exists — not be escaped, not be filtered, must raise.
    """
    _fresh()
    for name in ['x; DROP TABLE model_index;--', 'x--', 'x INTEGER; SELECT 1', '"x"',
                 'a b', '1abc', '/*x*/', '_leading_underscore']:
        assert not name.isidentifier() or name.startswith("_"), f"bad fixture: {name!r}"
        try:
            mi._safe_columns((name,))
        except RuntimeError:
            continue
        raise AssertionError(f"hostile ALTER name accepted: {name!r}")


def test_upgrade_adds_a_missing_column_with_the_type_schema_declares():
    """An existing database that predates a column must gain it as TEXT, matching a
    fresh install.

    Built honestly: create the table, DROP the column for real so the database genuinely
    predates it, then run init() and check the column came back with the type SCHEMA
    declares. A migration that guessed INTEGER for everything would leave an upgraded
    box with `sidecar_type INTEGER` while a fresh install has TEXT — same name, different
    storage class, and the only symptom would be surprising sort order.
    """
    _fresh()
    conn = db._conn()
    assert "sidecar_type" in {r[1] for r in conn.execute("PRAGMA table_info(model_index)")}
    conn.execute("ALTER TABLE model_index DROP COLUMN sidecar_type")
    conn.commit()
    assert "sidecar_type" not in {r[1] for r in conn.execute("PRAGMA table_info(model_index)")}

    mi.init()  # the upgrade path

    cols = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(model_index)")}
    assert cols.get("sidecar_type") == "TEXT", cols.get("sidecar_type")

    # And the migrated column actually works: a rescan fills it.
    d = paths.LLAMA_MODELS / "upgraded-sidecar"
    d.mkdir(parents=True, exist_ok=True)
    _write_fake_gguf(d / "m.gguf", {
        "block_count": 8, "attention.head_count": 4, "attention.head_count_kv": 2,
        "embedding_length": 256, "context_length": 4096,
    })
    side = d / "mmproj-m.gguf"
    _write_fake_gguf(side, {"block_count": 2})
    # general.type and clip.* are TOP-LEVEL keys, not architecture-prefixed ones:
    # _write_fake_gguf namespaces everything under `testarch.`, which is the wrong
    # place for them and would make this fixture assert nothing.
    # `general.type` and `clip.*` are TOP-LEVEL keys, not architecture-prefixed
    # ones: _write_fake_gguf namespaces everything under `testarch.`, which is the
    # wrong place for them. `general.type` is a STRING, so it cannot go through
    # _append_kv_key either — that helper writes bool(value), turning "mmproj" into
    # TRUE and making the assertion vacuous. Append it with its real type tag.
    raw = bytearray(side.read_bytes())
    n = struct.unpack_from("<Q", raw, 16)[0]
    struct.pack_into("<Q", raw, 16, n + 1)
    side.write_bytes(
        bytes(raw) + _enc_str("general.type") + struct.pack("<I", GGUF_STRING) + _enc_str("mmproj")
    )
    _append_kv_key(side, "clip.has_vision_encoder", True)
    mi.rescan()
    row = mi.rows()[0]
    assert row["sidecar_type"] == "mmproj", row["sidecar_type"]


def test_init_is_idempotent_when_nothing_is_missing():
    """Running init() twice must not ALTER anything or lose rows — the upgrade path runs
    on every process start, so a second run has to be a no-op.
    """
    _fresh()
    d = paths.LLAMA_MODELS / "idempotent"
    d.mkdir(parents=True, exist_ok=True)
    _write_fake_gguf(d / "m.gguf", {
        "block_count": 8, "attention.head_count": 4, "attention.head_count_kv": 2,
        "embedding_length": 256, "context_length": 4096,
    })
    mi.rescan()
    before = [(r["id"], r["bytes"]) for r in mi.rows()]
    schema_before = list(db._conn().execute("PRAGMA table_info(model_index)"))

    mi.init()
    mi.init()

    assert [(r["id"], r["bytes"]) for r in mi.rows()] == before
    assert list(db._conn().execute("PRAGMA table_info(model_index)")) == schema_before
