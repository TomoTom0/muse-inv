#!/usr/bin/env python3
"""muse-inv — Muse の仕組み・技術・環境の蓄積管理 CLI.

正本:
  data/muse-inv.db  SQLite データベース (items / item_dependencies / meta)。
  構築層 (built) とプラットフォーム層 (platform) を1つの items テーブルで持ち、
  layer 列で分ける。値域・必須・層と種別の対応は DB の CHECK 制約が強制する。
  JSONL は正本ではない。export の出力形式と、移行元スナップショット
  (data/*-export-2026-11-15.jsonl) としてのみ使う。

Python 3 標準ライブラリのみで動作する。
"""
import argparse
import datetime
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
DB_PATH = DATA / "muse-inv.db"
REGISTRY_JSONL = DATA / "registry-export-2026-11-15.jsonl"
PLATFORM_JSONL = DATA / "platform-export-2026-11-15.jsonl"
EXPORTS = BASE / "exports"
HOME = Path.home()

BUILT_KINDS = {"tool", "route", "procedure", "failure-pattern", "automation", "setting"}
PLATFORM_KINDS = {"skill", "connector", "doc", "goal", "cron", "core-file", "domain-file"}
STATUSES = {"active", "superseded", "unadopted", "unknown", "prepared"}
PORTABILITIES = {"portable", "conditional", "env-specific", "do-not-port", "unclassified"}
REQUIRED = ["id", "name", "kind", "layer", "status", "source", "description",
            "alias", "portability", "depends_on", "curated_in", "last_verified"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL CHECK (length(name) > 0),
    kind          TEXT NOT NULL,
    layer         TEXT NOT NULL CHECK (layer IN ('built', 'platform')),
    status        TEXT NOT NULL CHECK (status IN
                      ('active', 'superseded', 'unadopted', 'unknown', 'prepared')),
    source        TEXT NOT NULL CHECK (length(source) > 0),
    description   TEXT NOT NULL,
    alias         TEXT NOT NULL DEFAULT '',
    portability   TEXT NOT NULL CHECK (portability IN
                      ('portable', 'conditional', 'env-specific', 'do-not-port',
                       'unclassified')),
    curated_in    TEXT,
    last_verified TEXT CHECK (last_verified IS NULL OR last_verified GLOB
                      '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    inv_no        INTEGER UNIQUE,
    section       TEXT,
    status_note   TEXT,
    category      TEXT,
    CHECK (
        (layer = 'built'
         AND kind IN ('tool', 'route', 'procedure', 'failure-pattern',
                      'automation', 'setting')
         AND category IS NULL)
        OR
        (layer = 'platform'
         AND kind IN ('skill', 'connector', 'doc', 'goal', 'cron',
                      'core-file', 'domain-file')
         AND category IS NOT NULL
         AND inv_no IS NULL AND section IS NULL AND status_note IS NULL)
    ),
    CHECK (layer = 'platform' OR length(alias) > 0)
);
CREATE TABLE IF NOT EXISTS item_dependencies (
    item_id       TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    depends_on_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    ordinal       INTEGER NOT NULL,
    PRIMARY KEY (item_id, depends_on_id)
);
CREATE INDEX IF NOT EXISTS idx_items_layer ON items(layer);
CREATE INDEX IF NOT EXISTS idx_items_kind ON items(kind);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
CREATE INDEX IF NOT EXISTS idx_items_name ON items(name);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# ---------------------------------------------------------------- DB layer
def connect(create=False):
    """Open the registry DB. With create=True, create file + schema if absent."""
    if not DB_PATH.exists() and not create:
        print(f"ERROR: registry DB not found: {DB_PATH}\n"
              "  先に `muse-inv import-jsonl` (移行) か `muse-inv scan` / "
              "`muse-inv migrate` を実行してください。", file=sys.stderr)
        sys.exit(2)
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if create:
        conn.executescript(SCHEMA)
        conn.execute("INSERT OR IGNORE INTO meta(key, value) "
                     "VALUES ('schema_version', '1')")
        conn.commit()
    return conn


def row_to_record(conn, row):
    deps = [r[0] for r in conn.execute(
        "SELECT depends_on_id FROM item_dependencies WHERE item_id = ? "
        "ORDER BY ordinal", (row["id"],))]
    rec = {
        "id": row["id"], "name": row["name"], "kind": row["kind"],
        "layer": row["layer"], "status": row["status"], "source": row["source"],
        "description": row["description"], "alias": row["alias"],
        "portability": row["portability"], "depends_on": deps,
        "curated_in": row["curated_in"] or "",
        "last_verified": row["last_verified"] or "",
    }
    for opt in ("inv_no", "section", "status_note", "category"):
        if row[opt] is not None:
            rec[opt] = row[opt]
    return rec


def fetch_layer(conn, layer):
    rows = conn.execute(
        "SELECT * FROM items WHERE layer = ? ORDER BY rowid", (layer,)).fetchall()
    return [row_to_record(conn, r) for r in rows]


def fetch_all(conn):
    return fetch_layer(conn, "built"), fetch_layer(conn, "platform")


def insert_record(conn, rec):
    """Insert one validated record + its dependencies (caller commits)."""
    conn.execute(
        "INSERT INTO items (id, name, kind, layer, status, source, description,"
        " alias, portability, curated_in, last_verified, inv_no, section,"
        " status_note, category) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rec["id"], rec["name"], rec["kind"], rec["layer"], rec["status"],
         rec["source"], rec["description"], rec["alias"], rec["portability"],
         rec["curated_in"] or None, rec["last_verified"] or None,
         rec.get("inv_no"), rec.get("section"), rec.get("status_note"),
         rec.get("category")))
    for i, dep in enumerate(rec["depends_on"]):
        conn.execute(
            "INSERT INTO item_dependencies (item_id, depends_on_id, ordinal) "
            "VALUES (?,?,?)", (rec["id"], dep, i))


def upsert_record(conn, rec):
    """Insert or update one record in place (incoming dependency rows of other
    items are preserved; the record's own outgoing rows are replaced)."""
    conn.execute(
        "INSERT INTO items (id, name, kind, layer, status, source, description,"
        " alias, portability, curated_in, last_verified, inv_no, section,"
        " status_note, category) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(id) DO UPDATE SET name=excluded.name, kind=excluded.kind,"
        " layer=excluded.layer, status=excluded.status, source=excluded.source,"
        " description=excluded.description, alias=excluded.alias,"
        " portability=excluded.portability, curated_in=excluded.curated_in,"
        " last_verified=excluded.last_verified, inv_no=excluded.inv_no,"
        " section=excluded.section, status_note=excluded.status_note,"
        " category=excluded.category",
        (rec["id"], rec["name"], rec["kind"], rec["layer"], rec["status"],
         rec["source"], rec["description"], rec["alias"], rec["portability"],
         rec["curated_in"] or None, rec["last_verified"] or None,
         rec.get("inv_no"), rec.get("section"), rec.get("status_note"),
         rec.get("category")))
    conn.execute("DELETE FROM item_dependencies WHERE item_id = ?", (rec["id"],))
    for i, dep in enumerate(rec["depends_on"]):
        conn.execute(
            "INSERT INTO item_dependencies (item_id, depends_on_id, ordinal) "
            "VALUES (?,?,?)", (rec["id"], dep, i))


def replace_layer(conn, layer, records):
    """Replace one layer's contents with `records` inside a transaction:
    upsert the new set, delete ids that disappeared (their outgoing
    dependency rows cascade)."""
    with conn:
        old_ids = {r[0] for r in conn.execute(
            "SELECT id FROM items WHERE layer = ?", (layer,))}
        new_ids = {r["id"] for r in records}
        for rid in sorted(old_ids - new_ids):
            conn.execute("DELETE FROM items WHERE id = ?", (rid,))
        for rec in records:
            upsert_record(conn, rec)


# ---------------------------------------------------------------- validation
def validate_record(rec, where="record"):
    """Return list of error strings (empty = valid). Application-level check
    with readable messages; the DB CHECK constraints are the backstop."""
    errs = []
    if not isinstance(rec, dict):
        return [f"{where}: not a JSON object"]
    for f in REQUIRED:
        if f not in rec:
            errs.append(f"{where}: missing required field '{f}'")
    if errs:
        return errs
    if not isinstance(rec["id"], str) or not rec["id"]:
        errs.append(f"{where}: id must be a non-empty string")
    if not isinstance(rec["name"], str) or not rec["name"]:
        errs.append(f"{where}: name must be a non-empty string")
    layer = rec["layer"]
    if layer not in ("built", "platform"):
        errs.append(f"{where}: layer must be 'built' or 'platform', got {layer!r}")
    else:
        allowed = BUILT_KINDS if layer == "built" else PLATFORM_KINDS
        if rec["kind"] not in allowed:
            errs.append(f"{where}: kind {rec['kind']!r} not allowed for layer {layer} "
                        f"(allowed: {sorted(allowed)})")
    if rec["status"] not in STATUSES:
        errs.append(f"{where}: status {rec['status']!r} not in {sorted(STATUSES)}")
    if rec["portability"] not in PORTABILITIES:
        errs.append(f"{where}: portability {rec['portability']!r} not in {sorted(PORTABILITIES)}")
    if not isinstance(rec["source"], str) or not rec["source"]:
        errs.append(f"{where}: source must be a non-empty string")
    if not isinstance(rec["description"], str):
        errs.append(f"{where}: description must be a string")
    if not isinstance(rec["alias"], str):
        errs.append(f"{where}: alias must be a string")
    elif layer == "built" and rec["alias"] == "":
        errs.append(f"{where}: alias may be empty only for platform records")
    if not isinstance(rec["depends_on"], list) or not all(isinstance(x, str) for x in rec["depends_on"]):
        errs.append(f"{where}: depends_on must be a list of strings")
    if not isinstance(rec["curated_in"], str):
        errs.append(f"{where}: curated_in must be a string")
    lv = rec["last_verified"]
    if not isinstance(lv, str):
        errs.append(f"{where}: last_verified must be a string")
    elif lv != "":
        try:
            datetime.date.fromisoformat(lv)
        except ValueError:
            errs.append(f"{where}: last_verified {lv!r} is not an ISO date")
    return errs


def load_jsonl(path, label):
    """Load + validate a jsonl file (import-jsonl / import-check 用).
    Invalid lines are reported, never skipped silently."""
    records, errors = [], []
    if not path.exists():
        return records, [f"{label}: file not found: {path}"]
    seen = {}
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as e:
            errors.append(f"{label}:{i}: invalid JSON: {e}")
            continue
        errs = validate_record(rec, f"{label}:{i} ({rec.get('id', '?') if isinstance(rec, dict) else '?'})")
        errors.extend(errs)
        if isinstance(rec, dict) and rec.get("id"):
            if rec["id"] in seen:
                errors.append(f"{label}:{i}: duplicate id {rec['id']!r} (first at line {seen[rec['id']]})")
            seen[rec["id"]] = i
        if not errs:
            records.append(rec)
    return records, errors


def die_on_errors(errors):
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        print(f"{len(errors)} validation error(s); refusing to continue.", file=sys.stderr)
        sys.exit(2)


def canonical(rec):
    return json.dumps(rec, ensure_ascii=False, sort_keys=True)


def canonical_diff(rec):
    """Canonical form for the scan diff. scan regenerates last_verified as
    today's date on every run, so including it would report every platform
    record as changed on the first scan of a new day even when nothing in
    the environment changed; exclude it (canonical() keeps it)."""
    return json.dumps({k: v for k, v in rec.items() if k != "last_verified"},
                      ensure_ascii=False, sort_keys=True)


def today():
    return datetime.date.today().isoformat()


# ---------------------------------------------------------------- scan
def first_heading(path):
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if s.startswith("#"):
                return s.lstrip("#").strip()[:160]
            if s:
                return s[:160]
    except OSError:
        pass
    return ""


def slugify(s):
    s = s.replace("/", "-").replace("\\", "-")
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", s)
    return s.strip("-") or "item"


def scan_sources():
    """Enumerate the actual system. Returns (records, category_counts, missing_sources)."""
    records = []
    counts = Counter()
    missing = []

    def add(category, kind, rid, name, path):
        records.append({
            "id": rid, "name": name, "kind": kind, "layer": "platform",
            "status": "active", "source": str(path),
            "description": first_heading(path), "alias": "",
            "portability": "unclassified", "depends_on": [],
            "curated_in": "", "last_verified": today(), "category": category,
        })
        counts[category] += 1

    def glob_add(base, pattern, category, kind, id_fn, name_fn):
        if not base.exists():
            missing.append(f"{category}: base path missing: {base}")
            return
        files = sorted(base.glob(pattern))
        if not files:
            missing.append(f"{category}: no files matched {base}/{pattern}")
            return
        for f in files:
            if f.is_file():
                add(category, kind, id_fn(f), name_fn(f), f)

    docs = HOME / "docs"
    glob_add(docs, "**/*.md", "doc", "doc",
             lambda f: "platform:doc:" + slugify(str(f.relative_to(docs).with_suffix(""))),
             lambda f: str(f.relative_to(docs)))
    glob_add(Path("/opt/muse/skills"), "*/SKILL.md", "system-skill", "skill",
             lambda f: f"platform:skill:{f.parent.name}", lambda f: f.parent.name)
    glob_add(HOME / "workspace/custom-skills", "*/SKILL.md", "custom-skill", "skill",
             lambda f: f"platform:custom-skill:{f.parent.name}", lambda f: f.parent.name)
    glob_add(Path("/opt/hatch/connectors"), "*/CONNECTOR.md", "system-connector", "connector",
             lambda f: f"platform:connector:{f.parent.name}", lambda f: f.parent.name)
    glob_add(HOME / "workspace/custom-connectors", "*/CONNECTOR.md", "custom-connector", "connector",
             lambda f: f"platform:custom-connector:{f.parent.name}", lambda f: f.parent.name)
    goals = HOME / "workspace/goals"
    glob_add(goals, "*/GOAL.md", "goal", "goal",
             lambda f: f"platform:goal:{f.parent.name}", lambda f: f.parent.name)
    # cron: one record per file, from both cron.d and goals/*/crons
    cron_bases = [("cron", HOME / "workspace/cron.d", "**/*"),
                  ("cron", goals, "*/crons/**/*")]
    for category, base, pattern in cron_bases:
        if not base.exists():
            missing.append(f"cron: base path missing: {base}")
            continue
        for f in sorted(base.glob(pattern)):
            if f.is_file():
                rel = f.relative_to(HOME)
                add(category, "cron", "platform:cron:" + slugify(str(rel.with_suffix(""))),
                    f.name, f)
    for name in ["AGENTS.md", "SOUL.md", "IDENTITY.md", "USER.md", "MEMORY.md",
                 "TOOLS.md", "PROACTIVE_PREFERENCES.md"]:
        f = HOME / name
        if f.exists():
            add("core-file", "core-file", f"platform:core:{name}", name, f)
        else:
            missing.append(f"core-file: missing: {f}")
    glob_add(HOME / "memory/domains", "*.md", "domain-file", "domain-file",
             lambda f: f"platform:domain:{f.stem}", lambda f: f.name)
    glob_add(HOME / "memory/entities", "*.md", "entity-file", "domain-file",
             lambda f: f"platform:entity:{f.stem}", lambda f: f.name)
    return records, counts, missing


def cmd_scan(args):
    conn = connect(create=True)
    old_records = fetch_layer(conn, "platform")
    records, counts, missing = scan_sources()
    errs = []
    for r in records:
        errs.extend(validate_record(r, f"scan ({r['id']})"))
    die_on_errors(errs)
    ids = [r["id"] for r in records]
    dupes = [i for i, c in Counter(ids).items() if c > 1]
    if dupes:
        print(f"ERROR: scan produced duplicate ids: {dupes}", file=sys.stderr)
        sys.exit(2)
    registry = fetch_layer(conn, "built")
    collisions = sorted({r["id"] for r in records} & {r["id"] for r in registry})
    if collisions:
        print(f"ERROR: scan ids collide with built layer: {collisions}", file=sys.stderr)
        sys.exit(2)
    replace_layer(conn, "platform", records)
    # diff vs previous
    old_by_id = {r["id"]: r for r in old_records}
    new_by_id = {r["id"]: r for r in records}
    added = sorted(set(new_by_id) - set(old_by_id))
    removed = sorted(set(old_by_id) - set(new_by_id))
    changed = sorted(i for i in set(old_by_id) & set(new_by_id)
                     if canonical_diff(old_by_id[i]) != canonical_diff(new_by_id[i]))
    print(f"scan: wrote {len(records)} platform records -> {DB_PATH}")
    print("counts by category:")
    for cat in sorted(counts):
        print(f"  {cat}: {counts[cat]}")
    print(f"  TOTAL: {sum(counts.values())}")
    if missing:
        print("missing sources (reported, not fatal):")
        for m in missing:
            print(f"  - {m}")
    print(f"diff vs previous platform layer: added={len(added)} removed={len(removed)} changed={len(changed)}")
    for i in added:
        print(f"  + {i}")
    for i in removed:
        print(f"  - {i}")
    for i in changed:
        print(f"  ~ {i}")
    print(f"registry: {len(registry)} built records; "
          f"id collisions platform∩registry: {len(collisions)}")
    return 0


# ---------------------------------------------------------------- migrate
INVENTORY = (HOME / "workspace/goals/memory-structure-redesign/hidden_files"
             / "knowledge-inventory-2026-11-14.md")
ALIASES_SRC = (HOME / "workspace/goals/memory-structure-redesign/hidden_files"
               / "vector-index/aliases.json")


def normalize_status(raw):
    s = raw.strip()
    low = s.lower()
    if low.startswith("active"):
        return "active", (s if s != "active" else "")
    if low.startswith("prepared"):
        return "prepared", (s if s != "prepared" else "")
    if low.startswith("superseded"):
        return "superseded", s
    if low.startswith("unadopted"):
        return "unadopted", (s if s != "unadopted" else "")
    if low.startswith("unknown"):
        return "unknown", (s if s != "unknown" else "")
    return "unknown", s


def cmd_migrate(args):
    if not INVENTORY.exists():
        print(f"ERROR: inventory not found: {INVENTORY}", file=sys.stderr)
        return 2
    aliases = {}
    if ALIASES_SRC.exists():
        aliases = json.loads(ALIASES_SRC.read_text(encoding="utf-8"))
    text = INVENTORY.read_text(encoding="utf-8")
    section = ""
    per_section = Counter()
    records = []
    row_re = re.compile(r"^\|\s*(\d+)\s*\|")
    sec_re = re.compile(r"^## ([A-I])\.")
    for line in text.splitlines():
        m = sec_re.match(line)
        if m:
            section = m.group(1)
            continue
        m = row_re.match(line)
        if not m or not section:
            continue
        cells = [c.strip() for c in line.split("|")]
        # cells: ['', num, name, type, desc, source, status, curated, '']
        if len(cells) != 9:
            print(f"ERROR: unparsable row ({len(cells)} cells): {line[:120]}", file=sys.stderr)
            return 2
        num = int(cells[1])
        status, status_note = normalize_status(cells[6])
        rec = {
            "id": f"inv-{num:03d}", "name": cells[2], "kind": cells[3],
            "layer": "built", "status": status, "source": cells[5],
            "description": cells[4],
            "alias": aliases.get(str(num), ""),
            "portability": "unclassified", "depends_on": [],
            "curated_in": cells[7], "last_verified": "",
            "inv_no": num, "section": section,
        }
        if status_note:
            rec["status_note"] = status_note
        if not rec["alias"]:
            rec["alias"] = cells[4]  # fallback: description as alias (noted in README)
            rec["alias_note"] = "alias missing in aliases.json; description used as fallback"
        records.append(rec)
        per_section[section] += 1
    errs = []
    for r in records:
        errs.extend(validate_record(r, f"migrate ({r['id']})"))
    die_on_errors(errs)
    nums = [r["inv_no"] for r in records]
    if nums != sorted(nums) or len(set(nums)) != len(nums):
        print("ERROR: inventory numbers are not a clean sequence", file=sys.stderr)
        return 2
    conn = connect(create=True)
    replace_layer(conn, "built", records)
    print(f"migrate: parsed {len(records)} records -> {DB_PATH} (built layer)")
    print("by section: " + ", ".join(f"{s}:{per_section[s]}" for s in sorted(per_section)))
    print(f"number range: {min(nums)}..{max(nums)}, gaps: "
          f"{[n for n in range(min(nums), max(nums) + 1) if n not in set(nums)]}")
    print("note: the inventory's Summary counts states 151 (sections A-H). "
          "Section I (2026-11-15 gardener additions, #152-164) brings the file's "
          f"actual row total to {len(records)}; aliases.json also has {len(aliases)} keys.")
    print("portability: all 'unclassified', depends_on: all [] "
          "(the inventory does not state them; audit surfaces this as remaining work).")
    return 0


# ---------------------------------------------------------------- import-jsonl
def cmd_import_jsonl(args):
    """One-off storage migration: load the former JSONL source of truth into
    the SQLite DB. Refuses to overwrite an existing DB unless --force
    (the old DB is kept as muse-inv.db.bak-<timestamp>)."""
    registry, e1 = load_jsonl(Path(args.registry), "registry.jsonl")
    platform, e2 = load_jsonl(Path(args.platform), "platform.jsonl")
    die_on_errors(e1 + e2)
    overlap = sorted({r["id"] for r in registry} & {r["id"] for r in platform})
    if overlap:
        print(f"ERROR: id overlap between layers: {overlap}", file=sys.stderr)
        return 2
    if DB_PATH.exists():
        if not args.force:
            print(f"ERROR: DB already exists: {DB_PATH} "
                  "(再取り込みには --force。既存 DB は .bak として残ります)",
                  file=sys.stderr)
            return 2
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        bak = DB_PATH.with_name(f"muse-inv.db.bak-{stamp}")
        DB_PATH.rename(bak)
        print(f"import-jsonl: existing DB moved to {bak}")
    conn = connect(create=True)
    with conn:
        for rec in registry + platform:
            insert_record(conn, rec)
        conn.execute("INSERT OR REPLACE INTO meta(key, value) "
                     "VALUES ('imported_from', 'registry.jsonl + platform.jsonl')")
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('imported_at', ?)",
                     (datetime.datetime.now().isoformat(timespec="seconds"),))
    print(f"import-jsonl: {len(registry)} built + {len(platform)} platform "
          f"= {len(registry) + len(platform)} records -> {DB_PATH}")
    return 0


# ---------------------------------------------------------------- list/show
def load_all():
    conn = connect()
    try:
        return fetch_all(conn)
    finally:
        conn.close()


def cmd_list(args):
    registry, platform = load_all()
    pools = {"built": registry, "platform": platform, "all": registry + platform}
    rows = pools[args.layer]
    if args.kind:
        rows = [r for r in rows if r["kind"] == args.kind]
    if args.status:
        rows = [r for r in rows if r["status"] == args.status]
    for r in rows:
        print(f"{r['id']}\t{r['kind']}\t{r['status']}\t{r['portability']}\t{r['name']}")
    print(f"-- {len(rows)} record(s), layer={args.layer}"
          + (f", kind={args.kind}" if args.kind else "")
          + (f", status={args.status}" if args.status else ""))
    return 0


def cmd_show(args):
    registry, platform = load_all()
    for r in registry + platform:
        if r["id"] == args.id:
            print(json.dumps(r, ensure_ascii=False, indent=2))
            return 0
    print(f"ERROR: id not found: {args.id}", file=sys.stderr)
    return 1


# ---------------------------------------------------------------- search
_LATIN_RE = re.compile(r"[a-z0-9][a-z0-9._\-]*")
_JA_RUN_RE = re.compile(r"[぀-ヿ㐀-鿿]+")


def tokenize(text):
    latin = set(_LATIN_RE.findall(text.lower()))
    bigrams = set()
    for run in _JA_RUN_RE.findall(text):
        for i in range(len(run) - 1):
            bigrams.add(run[i:i + 2])
    return latin, bigrams


def keyword_score(q_lat, q_big, rec):
    name, alias, desc = rec["name"], rec["alias"], rec["description"]
    n_lat, n_big = tokenize(name)
    a_lat, a_big = tokenize(alias)
    t_lat, t_big = tokenize(f"{name} {alias} {desc} {rec['id']}")
    primary_lat, primary_big = n_lat | a_lat, n_big | a_big
    score = 0.0
    if q_lat:
        score += 0.30 * len(q_lat & primary_lat) / len(q_lat)
        score += 0.10 * len(q_lat & t_lat) / len(q_lat)
    if q_big:
        score += 0.60 * len(q_big & primary_big) / len(q_big)
        score += 0.10 * len(q_big & t_big) / len(q_big)
    for tok in q_lat:
        if len(tok) >= 3 and tok in n_lat:
            score += 0.25
    return min(score, 1.2)


def search_rank(query, records, top=None):
    q_lat, q_big = tokenize(query)
    scored = []
    for rec in records:
        s = keyword_score(q_lat, q_big, rec)
        # substring bonus: full latin tokens appearing verbatim in name/alias
        hay = (rec["name"] + " " + rec["alias"]).lower()
        for tok in q_lat:
            if len(tok) >= 3 and tok in hay:
                s += 0.15
        scored.append((s, rec))
    scored.sort(key=lambda x: -x[0])
    return scored[:top] if top else scored


def cmd_search(args):
    registry, platform = load_all()
    results = search_rank(args.query, registry + platform, top=args.top)
    for i, (s, r) in enumerate(results, 1):
        print(f"{i}. [{s:.3f}] ({r['layer']}/{r['kind']}) {r['id']} — {r['name']}")
        if r["alias"]:
            print(f"    alias: {r['alias'][:120]}")
        print(f"    source: {r['source'][:120]}")
    return 0


# ---------------------------------------------------------------- add
def cmd_add(args):
    rec = {
        "id": args.id, "name": args.name, "kind": args.kind, "layer": "built",
        "status": args.status, "source": args.source, "description": args.description,
        "alias": args.alias, "portability": args.portability,
        "depends_on": [x.strip() for x in args.depends_on.split(",") if x.strip()]
        if args.depends_on else [],
        "curated_in": args.curated_in, "last_verified": args.last_verified or "",
    }
    errs = validate_record(rec, f"add ({args.id})")
    if errs:
        for e in errs:
            print(f"ERROR: {e}", file=sys.stderr)
        return 2
    conn = connect()
    try:
        exists = conn.execute("SELECT 1 FROM items WHERE id = ?",
                              (args.id,)).fetchone()
        if exists:
            print(f"ERROR: id already exists: {args.id}", file=sys.stderr)
            return 2
        missing_deps = [d for d in rec["depends_on"]
                        if not conn.execute("SELECT 1 FROM items WHERE id = ?",
                                            (d,)).fetchone()]
        if missing_deps:
            print(f"ERROR: depends_on targets not in registry: {missing_deps}",
                  file=sys.stderr)
            return 2
        with conn:
            insert_record(conn, rec)
    finally:
        conn.close()
    print(json.dumps(rec, ensure_ascii=False, indent=2))
    print(f"add: inserted {args.id} into {DB_PATH}")
    return 0


# ---------------------------------------------------------------- audit
_DATE_MD_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\.md")
_GOAL_CRON_RE = re.compile(r"(goals/[\w./\-]+\.md)")
_DOMAIN_RE = re.compile(r"domains/([\w\-]+\.md)")
_ENTITY_RE = re.compile(r"entities/([\w\-]+\.md)")


def check_sources(registry):
    checked, broken, examples = 0, 0, []
    for r in registry:
        paths = set()
        for m in _DATE_MD_RE.findall(r["source"]):
            paths.add(HOME / "memory" / f"{m}.md")
        for m in _GOAL_CRON_RE.findall(r["source"]):
            paths.add(HOME / "workspace" / m)
        for m in _DOMAIN_RE.findall(r["source"]):
            paths.add(HOME / "memory/domains" / m)
        for m in _ENTITY_RE.findall(r["source"]):
            paths.add(HOME / "memory/entities" / m)
        for p in paths:
            checked += 1
            if not p.exists():
                broken += 1
                if len(examples) < 5:
                    examples.append(f"{r['id']}: {p}")
    return checked, broken, examples


def cmd_audit(args):
    registry, platform = load_all()
    print("== audit ==")
    print(f"registry (built): {len(registry)} records")
    print("  by kind: " + ", ".join(f"{k}:{v}" for k, v in sorted(Counter(r['kind'] for r in registry).items())))
    print("  by status: " + ", ".join(f"{k}:{v}" for k, v in sorted(Counter(r['status'] for r in registry).items())))
    daily_only = [r for r in registry if "daily-log only" in r["curated_in"]]
    print(f"  daily-log only (curated_in contains 'daily-log only'): {len(daily_only)}")
    uncl = [r for r in registry if r["portability"] == "unclassified"]
    print(f"  portability unclassified: {len(uncl)} / {len(registry)}")
    nodep = [r for r in registry if not r["depends_on"]]
    print(f"  depends_on empty: {len(nodep)} / {len(registry)}")
    noverify = [r for r in registry if not r["last_verified"]]
    print(f"  last_verified empty: {len(noverify)} / {len(registry)}")
    print(f"platform: {len(platform)} records")
    print("  by category: " + ", ".join(f"{k}:{v}" for k, v in sorted(Counter(r.get('category', '?') for r in platform).items())))
    print("  by kind: " + ", ".join(f"{k}:{v}" for k, v in sorted(Counter(r['kind'] for r in platform).items())))
    checked, broken, examples = check_sources(registry)
    print(f"sources: {checked} checkable local paths, {broken} broken")
    for e in examples:
        print(f"  broken: {e}")
    print(f"verdict: registry={len(registry)} platform={len(platform)}; "
          f"remaining classification work: portability {len(uncl)}件, "
          f"depends_on {len(nodep)}件, daily-log-only {len(daily_only)}件が未昇格")
    return 0


# ---------------------------------------------------------------- verify
VERIFY_CASES = [
    ("Xポストの全文を読む経路は？", "inv-098", "FxTwitter single-post API"),
    ("Tailscaleでubuntu-whiteに接続する経路は？", "inv-001", "Tailscale agent-dedicated tailnet"),
    ("yomiyasuとは何か", "inv-150", "yomiyasu"),
    ("SwitchBotのボタン名はどこから取れる？", "inv-080", "SwitchBot DIY IR button names via APK database"),
    ("FxTwitterのAPIでXポストを読む", "inv-098", "FxTwitter single-post API"),
    ("Amazonの商品ページが開けないときはどうする？", "inv-104", "Amazon.co.jp via white Chrome/CDP"),
    ("サーバー上で秘密情報をどう管理する？", "inv-083", "pass + mpass credential store on white"),
    ("経路検索のAPIはどこにある？", "inv-062", "route-api"),
]


def cmd_verify(args):
    registry, platform = load_all()
    allrecs = registry + platform
    print(f"{'query':<44} {'expected':<10} {'rank':>4}  verdict")
    passed = 0
    for query, exp_id, exp_name in VERIFY_CASES:
        ranked = search_rank(query, allrecs)
        rank = next((i for i, (s, r) in enumerate(ranked, 1) if r["id"] == exp_id), None)
        ok = rank is not None and rank <= 5
        passed += ok
        print(f"{query:<44} {exp_id:<10} {rank if rank else '-':>4}  "
              f"{'PASS' if ok else 'FAIL'}  ({exp_name})")
    print(f"verify: {passed}/{len(VERIFY_CASES)} PASS (expected item in top 5)")
    return 0 if passed == len(VERIFY_CASES) else 1


# ---------------------------------------------------------------- export / import-check
def cmd_export(args):
    registry, platform = load_all()
    pool = registry
    if args.portability:
        pool = [r for r in pool if r["portability"] == args.portability]
    by_id = {r["id"]: r for r in registry + platform}
    included = {r["id"]: r for r in pool}
    closure_added = []
    queue = list(included)
    while queue:
        cur = included[queue.pop()]
        for dep in cur["depends_on"]:
            if dep in by_id and dep not in included:
                included[dep] = by_id[dep]
                closure_added.append(dep)
                queue.append(dep)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(args.out) if args.out else (EXPORTS / stamp)
    out.mkdir(parents=True, exist_ok=True)
    items = [included[k] for k in sorted(included)]
    with (out / "items.jsonl").open("w", encoding="utf-8") as fh:
        for r in items:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    uncl = sum(1 for r in items if r["portability"] == "unclassified")
    manifest = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "filter_portability": args.portability or "(all)",
        "count": len(items),
        "closure_added": closure_added,
        "unclassified_count": uncl,
        "notes": [
            "depends_on は現行レジストリではほぼ空のため、依存閉包は実質未行使。"
            "portability unclassified の項目は移送可否が未判定であり、受け側で個別確認が要る。",
        ],
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [f"# export manifest — {manifest['generated_at']}", "",
             f"- filter: portability={manifest['filter_portability']}",
             f"- 件数: {len(items)}",
             f"- 依存閉包で追加された項目: {len(closure_added)}件"
             + (f" ({', '.join(closure_added)})" if closure_added else
                "（depends_on がほぼ空のため閉包は実質未行使）"),
             f"- portability unclassified: {uncl}件（移送可否未判定）", ""]
    for port in ["portable", "conditional", "env-specific", "do-not-port", "unclassified"]:
        group = [r for r in items if r["portability"] == port]
        if not group:
            continue
        lines.append(f"## {port} ({len(group)})")
        for r in group:
            lines.append(f"- [ ] {r['id']} — {r['name']} ({r['kind']}/{r['status']})")
        lines.append("")
    (out / "manifest.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"export: {len(items)} records -> {out}")
    print(f"  manifest.json / items.jsonl / manifest.md")
    print(f"  unclassified warnings: {uncl}; closure added: {len(closure_added)}")
    return 0


def cmd_import_check(args):
    target = Path(args.path)
    items_path = target / "items.jsonl" if target.is_dir() else target
    if not items_path.exists():
        print(f"ERROR: items file not found: {items_path}", file=sys.stderr)
        return 2
    incoming, errs = [], []
    for i, line in enumerate(items_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            incoming.append(json.loads(line))
        except json.JSONDecodeError as e:
            errs.append(f"{items_path}:{i}: {e}")
    if errs:
        for e in errs:
            print(f"ERROR: {e}", file=sys.stderr)
        return 2
    registry, platform = load_all()
    current = {r["id"]: r for r in registry + platform}
    present, missing, conflict = [], [], []
    for rec in incoming:
        cur = current.get(rec["id"])
        if cur is None:
            missing.append(rec["id"])
        elif canonical(cur) == canonical(rec):
            present.append(rec["id"])
        else:
            conflict.append(rec["id"])
    print(f"import-check: {items_path}")
    print(f"  total incoming: {len(incoming)}")
    print(f"  already present (identical): {len(present)}")
    print(f"  missing (would need setup): {len(missing)}")
    for i in missing:
        print(f"    - {i}")
    print(f"  conflicting (same id, different content): {len(conflict)}")
    for i in conflict:
        print(f"    ! {i}")
    return 0


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(prog="muse-inv",
                                 description="Muse の仕組み・技術・環境の蓄積管理 CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("scan", help="実物を列挙して platform 層を DB へ再生成し、差分を表示する")
    sub.add_parser("migrate", help="知識台帳 markdown を built 層へ移行する（ワンオフ）")
    p = sub.add_parser("import-jsonl", help="旧 JSONL 正本から SQLite DB へ取り込む（移行用）")
    p.add_argument("--registry", default=str(REGISTRY_JSONL))
    p.add_argument("--platform", default=str(PLATFORM_JSONL))
    p.add_argument("--force", action="store_true",
                   help="既存 DB を .bak へ退避してから取り込み直す")
    p = sub.add_parser("list", help="レコード一覧")
    p.add_argument("--layer", choices=["built", "platform", "all"], default="built")
    p.add_argument("--kind")
    p.add_argument("--status")
    p = sub.add_parser("show", help="1件を JSON で表示")
    p.add_argument("id")
    p = sub.add_parser("search", help="キーワード検索（名前・別名を重み付け）")
    p.add_argument("query")
    p.add_argument("--top", type=int, default=5)
    p = sub.add_parser("add", help="構築層へ1件登録（スキーマ検証あり）")
    p.add_argument("--id", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--kind", required=True, choices=sorted(BUILT_KINDS))
    p.add_argument("--status", required=True, choices=sorted(STATUSES))
    p.add_argument("--source", required=True)
    p.add_argument("--description", required=True)
    p.add_argument("--alias", required=True, dest="alias",
                   help="日本語の用途・別名（構築層では必須）")
    p.add_argument("--portability", required=True, choices=sorted(PORTABILITIES))
    p.add_argument("--curated-in", required=True)
    p.add_argument("--depends-on", default="")
    p.add_argument("--last-verified", default="")
    sub.add_parser("audit", help="母集団に対する網羅・未分類の監査")
    sub.add_parser("verify", help="固定テスト8問で検索順位を検証")
    p = sub.add_parser("export", help="積み下ろし用エクスポートを書き出す")
    p.add_argument("--portability", choices=sorted(PORTABILITIES))
    p.add_argument("--out")
    p = sub.add_parser("import-check", help="エクスポートと現行レジストリを照合")
    p.add_argument("path")

    args = ap.parse_args()
    fn = {"scan": cmd_scan, "migrate": cmd_migrate, "import-jsonl": cmd_import_jsonl,
          "list": cmd_list, "show": cmd_show, "search": cmd_search,
          "add": cmd_add, "audit": cmd_audit, "verify": cmd_verify,
          "export": cmd_export, "import-check": cmd_import_check}[args.cmd]
    return fn(args)


if __name__ == "__main__":
    sys.exit(main())
