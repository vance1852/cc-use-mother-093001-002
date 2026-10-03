"""文化素材权利链核验域的 SQLite 表结构。"""

from __future__ import annotations


RIGHTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS rworks (
    work_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    title TEXT NOT NULL,
    primary_creator_id TEXT NOT NULL REFERENCES actors(actor_id),
    current_stage TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rwork_versions (
    version_id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES rworks(work_id),
    version_no INTEGER NOT NULL,
    submitted_at TEXT NOT NULL,
    review_stage TEXT,
    locked INTEGER NOT NULL DEFAULT 0 CHECK(locked IN (0, 1)),
    locked_at TEXT,
    material_ids_json TEXT NOT NULL,
    snapshot_json TEXT,
    UNIQUE(work_id, version_no)
);
CREATE TABLE IF NOT EXISTS rights_holders (
    holder_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('holder','coauthor','both')),
    share_percent INTEGER NOT NULL DEFAULT 0 CHECK(share_percent BETWEEN 0 AND 100),
    contact_summary TEXT NOT NULL,
    can_accept_channel_deals INTEGER NOT NULL DEFAULT 0 CHECK(can_accept_channel_deals IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rmaterials (
    material_id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES rworks(work_id),
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    source_description TEXT NOT NULL,
    rights_holder_ids_json TEXT NOT NULL,
    holder_shares_json TEXT NOT NULL,
    coauthor_ids_json TEXT NOT NULL,
    territory TEXT NOT NULL,
    purposes_json TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT,
    required_attribution TEXT NOT NULL,
    alternative_material_id TEXT,
    evidence_ids_json TEXT NOT NULL,
    registered_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revidence (
    evidence_id TEXT PRIMARY KEY,
    material_id TEXT NOT NULL REFERENCES rmaterials(material_id),
    filename TEXT NOT NULL,
    media_type TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    summary TEXT NOT NULL,
    classification TEXT NOT NULL DEFAULT 'normal' CHECK(classification IN ('normal','sensitive')),
    uploaded_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
-- 同一素材下相同内容哈希只允许登记一次（幂等去重的持久约束）。
CREATE UNIQUE INDEX IF NOT EXISTS idx_revidence_material_hash
    ON revidence(material_id, sha256);
-- 全库哈希索引：相同证据重复上传时返回既有记录。
CREATE UNIQUE INDEX IF NOT EXISTS idx_revidence_hash ON revidence(sha256);
CREATE TABLE IF NOT EXISTS rverification_tasks (
    task_id TEXT PRIMARY KEY,
    material_id TEXT NOT NULL REFERENCES rmaterials(material_id),
    status TEXT NOT NULL CHECK(status IN ('pending','authentic','inauthentic','withdrawn')),
    verifier_id TEXT REFERENCES actors(actor_id),
    reviewer_id TEXT REFERENCES actors(actor_id),
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rconclusions (
    conclusion_id TEXT PRIMARY KEY,
    material_id TEXT NOT NULL REFERENCES rmaterials(material_id),
    sequence_no INTEGER NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN (
        'cleared','restricted','blocked','expired','revoked','withdrawn')),
    trigger TEXT NOT NULL CHECK(trigger IN (
        'verification','supplement','expiry','revocation','withdrawal','conflict_resolution')),
    basis_json TEXT NOT NULL,
    basis_hash TEXT NOT NULL,
    supersedes_id TEXT REFERENCES rconclusions(conclusion_id),
    issued_by TEXT NOT NULL REFERENCES actors(actor_id),
    issued_at TEXT NOT NULL,
    usable INTEGER NOT NULL CHECK(usable IN (0, 1)),
    UNIQUE(material_id, sequence_no)
);
CREATE TABLE IF NOT EXISTS rreviews (
    work_id TEXT NOT NULL REFERENCES rworks(work_id),
    stage TEXT NOT NULL,
    version_id TEXT NOT NULL REFERENCES rwork_versions(version_id),
    entered_at TEXT NOT NULL,
    PRIMARY KEY (work_id, stage)
);
CREATE TABLE IF NOT EXISTS rdeals (
    deal_id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES rworks(work_id),
    partner TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('proposed','signed','blocked','cancelled')),
    requires_purposes_json TEXT NOT NULL,
    territory TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rdisputes (
    dispute_id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES rworks(work_id),
    material_id TEXT REFERENCES rmaterials(material_id),
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'filed','preserved','partially_frozen','settled','adjudicated','rejected')),
    filed_by TEXT NOT NULL REFERENCES actors(actor_id),
    filed_at TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    resolution_json TEXT NOT NULL DEFAULT '{}'
);
-- 异议案件下的证据保全清单（哈希固定后不可更改）。
CREATE TABLE IF NOT EXISTS rdispute_preservations (
    preservation_id TEXT PRIMARY KEY,
    dispute_id TEXT NOT NULL REFERENCES rdisputes(dispute_id),
    evidence_id TEXT REFERENCES revidence(evidence_id),
    sha256 TEXT NOT NULL,
    summary TEXT NOT NULL,
    preserved_at TEXT NOT NULL,
    UNIQUE(dispute_id, evidence_id)
);
-- 局部冻结范围：冻结某个作品版本对某素材的评审使用。
CREATE TABLE IF NOT EXISTS rfreezes (
    freeze_id TEXT PRIMARY KEY,
    dispute_id TEXT NOT NULL REFERENCES rdisputes(dispute_id),
    version_id TEXT NOT NULL REFERENCES rwork_versions(version_id),
    material_id TEXT REFERENCES rmaterials(material_id),
    lifted INTEGER NOT NULL DEFAULT 0 CHECK(lifted IN (0, 1)),
    created_at TEXT NOT NULL
);
"""
