"""定义权利链核验使用的角色、状态与业务词汇。"""

# 角色：前四个来自基础服务，其余为权利链核验扩展。
ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"
ROLE_REVIEWER = "reviewer"              # 冲突复核员
ROLE_AUDITOR = "auditor"
ROLE_CREATOR = "creator"                # 创作者 / 投稿人
ROLE_VERIFIER = "verifier"              # 真实性核验员
ROLE_JUDGE = "judge"                    # 评委
ROLE_LICENSING = "licensing_officer"    # 授权专员
ROLE_CASE_HANDLER = "case_handler"      # 异议处理员

# 可以登记素材、作品、版本与补交证明的角色。
REGISTER_ROLES = frozenset({ROLE_ADMIN, ROLE_OPERATOR, ROLE_CREATOR})
# 可以读取敏感证明内容的角色；评委与无关人员不在其中。
SENSITIVE_READ_ROLES = frozenset({ROLE_ADMIN, ROLE_LICENSING, ROLE_VERIFIER, ROLE_CASE_HANDLER})
# 可以查看待核验队列的角色。
QUEUE_ROLES = frozenset({ROLE_ADMIN, ROLE_AUDITOR, ROLE_LICENSING, ROLE_VERIFIER, ROLE_REVIEWER})
# 可以查看许可失效影响分析的角色。
IMPACT_ROLES = frozenset({ROLE_ADMIN, ROLE_OPERATOR, ROLE_LICENSING, ROLE_REVIEWER})
# 可以处理异议（保全、冻结、和解、裁定）的角色。
CASE_ROLES = frozenset({ROLE_ADMIN, ROLE_CASE_HANDLER})
# 可以追溯结论完整依据的角色（授权专员）。
OFFICER_ROLES = frozenset({ROLE_ADMIN, ROLE_LICENSING})
# 可以撤回许可的角色。
LICENSE_ROLES = frozenset({ROLE_ADMIN, ROLE_LICENSING})
# 可以发起商业合作的角色。
COLLAB_CREATE_ROLES = frozenset({ROLE_ADMIN, ROLE_OPERATOR, ROLE_LICENSING})
# 可以执行恢复一致性校验的角色。
RECOVERY_ROLES = frozenset({ROLE_ADMIN, ROLE_AUDITOR, ROLE_LICENSING})

# 核验任务类型与对应职责角色。
TASK_AUTHENTICITY = "authenticity"      # 真实性核验
TASK_CONFLICT = "conflict_review"       # 冲突复核
TASK_TYPES = frozenset({TASK_AUTHENTICITY, TASK_CONFLICT})
TASK_ROLE = {TASK_AUTHENTICITY: ROLE_VERIFIER, TASK_CONFLICT: ROLE_REVIEWER}
TASK_PENDING = "pending"
TASK_APPROVED = "approved"
TASK_REJECTED = "rejected"

# 素材核验生命周期。
MATERIAL_PENDING = "pending_verification"
MATERIAL_VERIFIED = "verified"
MATERIAL_REJECTED = "rejected"

# 许可状态。
LICENSE_ACTIVE = "active"
LICENSE_REVOKED = "revoked"
LICENSE_EXPIRED = "expired"

# 评审阶段：draft 之后逐级推进。
STAGE_DRAFT = "draft"
STAGE_PRELIMINARY = "preliminary"
STAGE_SEMI_FINAL = "semi_final"
STAGE_FINAL = "final"
REVIEW_STAGES = frozenset({STAGE_PRELIMINARY, STAGE_SEMI_FINAL, STAGE_FINAL})
STAGE_ORDER = {STAGE_DRAFT: 0, STAGE_PRELIMINARY: 1, STAGE_SEMI_FINAL: 2, STAGE_FINAL: 3}

# 权利结论的存续状态：current 为当前结论，superseded 被新结论取代，
# invalidated 被裁定否定；历史结论的依据快照永远保留。
STANDING_CURRENT = "current"
STANDING_SUPERSEDED = "superseded"
STANDING_INVALIDATED = "invalidated"

# 异议状态机：filed → preserved → frozen → settled / adjudicated。
OBJECTION_FILED = "filed"
OBJECTION_PRESERVED = "preserved"
OBJECTION_FROZEN = "frozen"
OBJECTION_SETTLED = "settled"
OBJECTION_ADJUDICATED = "adjudicated"
OBJECTION_OPEN = frozenset({OBJECTION_FILED, OBJECTION_PRESERVED, OBJECTION_FROZEN})
OBJECTION_TARGETS = frozenset({"material", "version", "conclusion", "work", "collaboration"})
ADJUDICATION_OUTCOMES = frozenset({"dismissed", "sustained"})

# 冻结状态。
FREEZE_ACTIVE = "active"
FREEZE_LIFTED = "lifted"

# 商业合作状态。
COLLAB_NEGOTIATING = "negotiating"      # 尚未签署
COLLAB_SIGNED = "signed"
