# 核验文化素材权利链协作基础服务

本项目提供文化创意赛事与成果转化业务共享的服务端基础能力，负责项目机构、业务节点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。各领域模块可以在这些稳定边界上扩展自己的状态、规则和接口。

仓库内包含两个包：

- `creative_program_foundation`：上述基础服务；
- `rights_chain`：文化素材权利链核验系统，覆盖素材登记、双职责核验、不可改写的权利结论、许可失效影响分析、异议处理、敏感证明分级读取与中断恢复校验。

## 目录

- src/creative_program_foundation/：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- src/rights_chain/：权利链常量、扩展表结构、领域服务、HTTP 路由和离线验收；
- tests/：基础规则、事务边界、接口路由、权利链规则和端到端验收测试。

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
    PYTHONPATH=src python3 -m rights_chain.acceptance

基础验收在临时库中登记机构、操作者、节点和参考资料并核对幂等回执与审计链；权利链验收复现茶礼作品场景：登记老照片、书法拓片、社区口述三类素材，完成真实性核验与冲突复核后进入复赛评审，随后经历异议立案、证据保全、局部冻结、和解、许可撤回与补交、渠道合作签署，最后模拟服务中断恢复，全部检查通过时输出一行 ok 为 true 的 JSON 并以退出码 0 结束。

## 权利链核验规则摘要

- 创作者登记素材来源、权利人与共同创作份额（合计必须为 100）、许可地域与用途、商业化期限、必需署名、证明文件摘要及替代素材；登记后自动生成真实性核验与冲突复核两项任务，分属核验员与复核员，且不能由同一人或登记人本人完成；
- 作品版本进入评审时生成第一份权利结论，结论依据（许可状态、核验记录、证据哈希）以快照和哈希固化；此后补交或撤回授权只会生成新的结论，历史结论及其依据永不改写；
- 许可被撤回或到期时，系统报告受影响的作品版本、所在评审阶段和尚未签署的商业合作，并刷新相关版本的当前结论；
- 异议支持立案、证据保全（哈希快照幂等）、按对象局部冻结、和解与裁定；裁定成立可使素材或结论失效并联动生成新结论；
- 敏感证明内容仅管理员、授权专员、核验员、异议处理员和上传者本人可读，评委等无关人员只能看到哈希；授权专员可通过 GET /rights/conclusions/{id}/basis 追溯每个结论的完整依据链；
- 相同证明内容重复上传按内容哈希幂等去重；所有写接口支持 request_id 幂等回放；GET /rights/recovery-check 在服务恢复后校验待核验队列、证据哈希、保全快照、结论依据与审计链的一致性。

## HTTP 服务

    PYTHONPATH=src python3 -m creative_program_foundation.api --database creative_program.sqlite3 --host 127.0.0.1 --port 8080
    PYTHONPATH=src python3 -m rights_chain.api --database rights_chain.sqlite3 --host 127.0.0.1 --port 8080

健康检查使用 GET /health。写入接口通过 X-Actor-Id 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。权利链接口统一挂在 /rights 前缀下：素材 /rights/materials、许可 /rights/licenses、核验队列 /rights/verification-tasks、作品 /rights/works、版本 /rights/versions、结论 /rights/conclusions、异议 /rights/objections、合作 /rights/collaborations、恢复校验 /rights/recovery-check。
