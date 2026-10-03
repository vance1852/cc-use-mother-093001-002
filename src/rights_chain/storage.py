"""在基础数据库上扩展权利链核验所需的表结构。"""

from __future__ import annotations

from pathlib import Path

from creative_program_foundation.storage import Database


RIGHTS_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS rights_works (
    work_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    title TEXT NOT NULL,
    representative_id TEXT REFERENCES actors(actor_id),
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rights_work_members (
    work_id TEXT NOT NULL REFERENCES rights_works(work_id),
    member_id TEXT NOT NULL REFERENCES actors(actor_id),
    share_percent INTEGER NOT NULL CHECK(share_percent > 0 AND share_percent <= 100),
    PRIMARY KEY(work_id, member_id)
);
CREATE TABLE IF NOT EXISTS rights_materials (
    material_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    work_id TEXT NOT NULL REFERENCES rights_works(work_id),
    title TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_description TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending_verification', 'verified', 'rejected')),
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rights_material_holders (
    material_id TEXT NOT NULL REFERENCES rights_materials(material_id),
    holder_name TEXT NOT NULL,
    share_percent INTEGER NOT NULL CHECK(share_percent > 0 AND share_percent <= 100),
    PRIMARY KEY(material_id, holder_name)
);
CREATE TABLE IF NOT EXISTS rights_material_alternatives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    material_id TEXT NOT NULL REFERENCES rights_materials(material_id),
    alternative_material_id TEXT REFERENCES rights_materials(material_id),
    note TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rights_licenses (
    license_id TEXT PRIMARY KEY,
    material_id TEXT NOT NULL REFERENCES rights_materials(material_id),
    territory TEXT NOT NULL,
    usage_scope TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT,
    required_attribution TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'revoked', 'expired')),
    submitted_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    invalidated_at TEXT,
    invalidate_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_rights_licenses_material ON rights_licenses(material_id, status);
CREATE TABLE IF NOT EXISTS rights_evidence (
    evidence_id TEXT PRIMARY KEY,
    material_id TEXT NOT NULL REFERENCES rights_materials(material_id),
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    summary TEXT NOT NULL,
    sensitive INTEGER NOT NULL CHECK(sensitive IN (0, 1)),
    uploaded_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(material_id, content_hash)
);
CREATE TABLE IF NOT EXISTS rights_verification_tasks (
    task_id TEXT PRIMARY KEY,
    material_id TEXT NOT NULL REFERENCES rights_materials(material_id),
    task_type TEXT NOT NULL CHECK(task_type IN ('authenticity', 'conflict_review')),
    status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'rejected')),
    decided_by TEXT,
    decided_at TEXT,
    decision_note TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(material_id, task_type)
);
CREATE INDEX IF NOT EXISTS idx_rights_tasks_status ON rights_verification_tasks(status, task_type);
CREATE TABLE IF NOT EXISTS rights_work_versions (
    version_id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES rights_works(work_id),
    version_no INTEGER NOT NULL CHECK(version_no >= 1),
    stage TEXT NOT NULL CHECK(stage IN ('draft', 'preliminary', 'semi_final', 'final')),
    entered_review_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(work_id, version_no)
);
CREATE TABLE IF NOT EXISTS rights_version_materials (
    version_id TEXT NOT NULL REFERENCES rights_work_versions(version_id),
    material_id TEXT NOT NULL REFERENCES rights_materials(material_id),
    PRIMARY KEY(version_id, material_id)
);
CREATE INDEX IF NOT EXISTS idx_rights_version_materials_material ON rights_version_materials(material_id);
CREATE TABLE IF NOT EXISTS rights_conclusions (
    conclusion_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES rights_work_versions(version_id),
    sequence INTEGER NOT NULL CHECK(sequence >= 1),
    standing TEXT NOT NULL CHECK(standing IN ('current', 'superseded', 'invalidated')),
    usable INTEGER NOT NULL CHECK(usable IN (0, 1)),
    reason TEXT NOT NULL,
    basis_json TEXT NOT NULL,
    basis_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(version_id, sequence)
);
CREATE TABLE IF NOT EXISTS rights_objections (
    objection_id TEXT PRIMARY KEY,
    target_type TEXT NOT NULL CHECK(target_type IN ('material', 'version', 'conclusion', 'work', 'collaboration')),
    target_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('filed', 'preserved', 'frozen', 'settled', 'adjudicated')),
    filed_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    resolution TEXT,
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS rights_preservations (
    objection_id TEXT NOT NULL REFERENCES rights_objections(objection_id),
    evidence_id TEXT NOT NULL REFERENCES rights_evidence(evidence_id),
    content_hash TEXT NOT NULL,
    preserved_by TEXT NOT NULL,
    preserved_at TEXT NOT NULL,
    PRIMARY KEY(objection_id, evidence_id)
);
CREATE TABLE IF NOT EXISTS rights_freezes (
    freeze_id TEXT PRIMARY KEY,
    objection_id TEXT NOT NULL REFERENCES rights_objections(objection_id),
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    scope_note TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'lifted')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    lifted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_rights_freezes_target ON rights_freezes(target_type, target_id, status);
CREATE TABLE IF NOT EXISTS rights_collaborations (
    collaboration_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES rights_work_versions(version_id),
    partner TEXT NOT NULL,
    channel TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('negotiating', 'signed')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    signed_by TEXT,
    signed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_rights_collaborations_version ON rights_collaborations(version_id, status);
"""


class RightsDatabase(Database):
    """在基础服务库结构上追加权利链核验表。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        super().__init__(path)
        self.connection.executescript(RIGHTS_SCHEMA)
