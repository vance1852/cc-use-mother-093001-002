"""文化素材权利链核验域使用的不可变数据对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Work:
    """参赛作品（可含多个评审版本）。"""

    work_id: str
    site_id: str
    title: str
    primary_creator_id: str
    current_stage: str
    created_at: str


@dataclass(frozen=True)
class WorkVersion:
    """作品的一个不可变评审版本。"""

    version_id: str
    work_id: str
    version_no: int
    submitted_at: str
    review_stage: str
    locked: bool
    locked_at: str | None
    material_ids: tuple[str, ...]
    # 进入评审时固化的结论快照：{material_id: {"conclusion_id", "decision", "basis_hash"}}
    snapshot: dict[str, dict[str, str]]


@dataclass(frozen=True)
class Material:
    """作品引用的文化素材（老照片、书法拓片、口述故事等）。"""

    material_id: str
    work_id: str
    kind: str
    title: str
    source_description: str
    rights_holder_ids: tuple[str, ...]
    holder_shares: dict[str, int]
    coauthor_ids: tuple[str, ...]
    territory: str
    purposes: tuple[str, ...]
    valid_from: str
    valid_until: str | None
    required_attribution: str
    alternative_material_id: str | None
    evidence_ids: tuple[str, ...]
    registered_by: str
    created_at: str


@dataclass(frozen=True)
class RightsHolder:
    """素材权利人或联合作者。

    share_percent 仅为登记时的参考默认值；共同创作份额按每件素材分别约定，
    以 Material.holder_shares 为准。
    """

    holder_id: str
    display_name: str
    kind: str  # holder | coauthor | both
    share_percent: int
    contact_summary: str
    can_accept_channel_deals: bool
    created_at: str


@dataclass(frozen=True)
class Evidence:
    """授权证明文件（只存摘要与哈希，敏感正文不入库）。"""

    evidence_id: str
    material_id: str
    filename: str
    media_type: str
    sha256: str
    size_bytes: int
    summary: str
    classification: str  # normal | sensitive
    uploaded_by: str
    created_at: str


@dataclass(frozen=True)
class VerificationTask:
    """真实性核验任务。"""

    task_id: str
    material_id: str
    status: str  # pending | authentic | inauthentic | withdrawn
    verifier_id: str | None
    reviewer_id: str | None
    created_at: str
    updated_at: str
    notes: str


@dataclass(frozen=True)
class RightsConclusion:
    """对一份素材在某时点的权利结论（追加式，永不修改）。"""

    conclusion_id: str
    material_id: str
    sequence_no: int
    decision: str  # cleared | restricted | blocked | expired | revoked | withdrawn
    trigger: str  # verification | supplement | expiry | revocation | withdrawal | conflict_resolution
    basis: dict[str, Any]
    basis_hash: str
    supersedes_id: str | None
    issued_by: str
    issued_at: str
    usable: bool


@dataclass(frozen=True)
class ReviewEntry:
    """评审阶段记录；阶段进入即冻结当时依据。"""

    work_id: str
    stage: str
    version_id: str
    entered_at: str


@dataclass(frozen=True)
class CommercialDeal:
    """尚未签署的商业/渠道合作意向。"""

    deal_id: str
    work_id: str
    partner: str
    status: str  # proposed | signed | blocked | cancelled
    requires_purposes: tuple[str, ...]
    territory: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Dispute:
    """异议案件。"""

    dispute_id: str
    work_id: str
    material_id: str | None
    reason: str
    status: str  # filed | preserved | partially_frozen | settled | adjudicated | rejected
    filed_by: str
    filed_at: str
    scope: dict[str, Any]
    resolution: dict[str, Any] = field(default_factory=dict)
