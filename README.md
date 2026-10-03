# 核验文化素材权利链协作基础服务

本项目提供文化创意赛事与成果转化业务共享的服务端基础能力，负责项目机构、业务节点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。各领域模块可以在这些稳定边界上扩展自己的状态、规则和接口。

`creative_program_foundation.rights_*` 模块在该基础上实现**文化素材权利链核验域**：创作者登记素材来源、权利人、共同创作份额、许可地域与用途、期限、必需署名、证明文件摘要及替代素材；授权专员完成真实性核验，冲突复核员独立复核；作品版本进入评审时冻结当时结论依据，事后补交/撤回只追加新结论；许可失效时给出受影响版本、评审阶段与尚未签署商业合作的影响面；支持异议立案、证据保全、局部冻结、和解或裁定，敏感证明对评委脱敏，授权专员可通过 API 追溯每个结论的完整依据。

## 目录

- src/creative_program_foundation/：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
  - `rights_models.py` / `rights_schema.py`：权利链域数据对象与表结构；
  - `rights_service.py`：登记、核验复核、结论追加链、评审冻结、影响分析、异议全流程；
  - `rights_api.py`：权利链 HTTP/JSON 边界（未命中路由回落基础服务）；
  - `rights_acceptance.py`：茶礼作品端到端离线验收。
- tests/：基础规则、事务边界、接口路由、权利链域和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests -v

## 构建检查

    python3 -m compileall -q src tests

## 离线验收

    PYTHONPATH=src python3 -m creative_program_foundation.acceptance
    PYTHONPATH=src python3 -m creative_program_foundation.rights_acceptance

权利链验收以一件同时引用老照片、书法拓片、社区口述故事的茶礼作品为场景，覆盖证据哈希幂等、核验与冲突复核、评审依据冻结、补交授权只追加、许可到期影响面、异议立案/保全/局部冻结/和解、评委脱敏、专员追溯及重启后队列与哈希一致性，成功时输出一行 status 为 ok 的 JSON 并以退出码 0 结束。

## HTTP 服务

    PYTHONPATH=src python3 -m creative_program_foundation.api --database creative_program.sqlite3 --host 127.0.0.1 --port 8080
    PYTHONPATH=src python3 -m creative_program_foundation.rights_api --database rights.sqlite3 --host 127.0.0.1 --port 8080

健康检查使用 GET /health。写入接口通过 X-Actor-Id 标识操作者，服务重启后 SQLite 中的业务状态、待核验队列、证据哈希和审计历史继续保留。

### 权利链域主要接口

- `POST /rworks`、`POST /rights-holders`、`POST /rmaterials`：作品、权利人（含渠道合作代表权标志）、素材（份额合计 100、地域/用途/期限/署名/替代素材）；
- `POST /revidence`：证明文件，仅存摘要与 SHA-256（可传 content_base64 由服务计算），相同内容重复上传幂等；`classification=sensitive` 的证明对 judge/reviewer 脱敏；
- `POST /verification-tasks`、`.../submit`、`.../conflict-review-request`、`.../conflict-review`、`GET /verification-tasks/pending`：真实性核验与职责分离的冲突复核（复核人不得就是核验人）；
- `GET /rconclusions?material_id=`、`POST /rconclusions/supplement|revoke|withdraw|sweep-expired`：结论只追加不改写（cleared/restricted/blocked/expired/revoked/withdrawn）；
- `POST /rwork-versions`、`POST /rreviews/enter`、`GET /review-pack?work_id=`：版本进入评审时固化结论编号与依据哈希；评委评审包自动脱敏；
- `GET /rtrace?material_id=`（或 conclusion_id）：授权专员/复核员/审计追溯结论链、完整依据、证据哈希与依赖该素材的已冻结版本；`GET /impact?material_id=` 给出失效影响面；
- `POST /rdeals`、`.../sign`、`.../block`：商业合作；存在局部冻结、相关素材无可用授权或团队无获授权代表时拒绝签署；
- `POST /rdisputes`、`.../preserve`、`.../freeze`、`.../resolve`：异议立案、证据保全（哈希独立固定）、按版本/素材的局部冻结、和解/裁定（解除冻结并可追加结论）。

