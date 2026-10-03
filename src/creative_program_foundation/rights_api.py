"""文化素材权利链核验域的 HTTP/JSON 路由与服务启动入口。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import api as base_api
from .errors import DomainError, ValidationError
from .rights_schema import RIGHTS_SCHEMA
from .rights_service import RightsService
from .storage import Database


def _dataclass_list(items: list[Any]) -> list[dict[str, Any]]:
    return [item.__dict__ for item in items]


def _receipt(receipt: Any) -> dict[str, Any]:
    """把幂等收据与业务返回详情合并为单个响应对象。"""

    return {**receipt.__dict__, **(receipt.response or {})}


def rights_route(service: RightsService, method: str, path: str, body: dict[str, Any],
                 headers: dict[str, str]) -> tuple[int, dict[str, Any]] | None:
    """处理权利链路由；不属于本域时返回 None 回落到基础服务。"""

    parsed = urlparse(path)
    p = parsed.path.rstrip("/") or "/"
    query = parse_qs(parsed.query)
    actor_id = headers.get("X-Actor-Id", "")

    def q(name: str, default: str = "") -> str:
        return query.get(name, [default])[0]

    try:
        # ------------------------------------------------ 作品 / 权利人 / 素材
        if method == "POST" and p == "/rworks":
            r = service.register_work(actor_id=actor_id, **body)
            return 200 if r.replayed else 201, _receipt(r)
        if method == "GET" and p.startswith("/rworks/"):
            return 200, service.get_work(p.rsplit("/", 1)[1]).__dict__
        if method == "POST" and p == "/rights-holders":
            r = service.register_rights_holder(actor_id=actor_id, **body)
            return 200 if r.replayed else 201, _receipt(r)
        if method == "POST" and p == "/rmaterials":
            r = service.register_material(actor_id=actor_id, **body)
            return 200 if r.replayed else 201, _receipt(r)
        if method == "GET" and p.startswith("/rmaterials/"):
            material_id = p.rsplit("/", 1)[1]
            if material_id == "":
                raise ValidationError("material_id 不能为空")
            payload: dict[str, Any] = service.get_material(material_id).__dict__
            payload["conclusions"] = [c.__dict__ for c in service.list_conclusions(material_id)]
            payload["evidence"] = service.list_evidence(material_id, actor_id)
            return 200, payload

        # ------------------------------------------------------------- 证据
        if method == "POST" and p == "/revidence":
            r = service.upload_evidence(actor_id=actor_id, **body)
            return 200 if r.replayed else 201, _receipt(r)
        if method == "GET" and p == "/revidence":
            material_id = q("material_id")
            if not material_id:
                raise ValidationError("material_id 不能为空")
            return 200, {"items": service.list_evidence(material_id, actor_id)}

        # --------------------------------------------------------- 核验/复核
        if method == "POST" and p == "/verification-tasks":
            r = service.create_verification_task(actor_id=actor_id, **body)
            return 200 if r.replayed else 201, _receipt(r)
        if method == "GET" and p == "/verification-tasks/pending":
            return 200, {"items": _dataclass_list(service.list_pending_tasks())}
        if method == "POST" and p.startswith("/verification-tasks/") and p.endswith("/submit"):
            task_id = p.split("/")[2]
            r = service.submit_verification(actor_id=actor_id, task_id=task_id, **body)
            return 200 if r.replayed else 201, _receipt(r)
        if method == "POST" and p.startswith("/verification-tasks/") and p.endswith("/conflict-review-request"):
            task_id = p.split("/")[2]
            r = service.request_conflict_review(actor_id=actor_id, task_id=task_id, **body)
            return 200 if r.replayed else 201, _receipt(r)
        if method == "POST" and p.startswith("/verification-tasks/") and p.endswith("/conflict-review"):
            task_id = p.split("/")[2]
            r = service.resolve_conflict_review(actor_id=actor_id, task_id=task_id, **body)
            return 200 if r.replayed else 201, _receipt(r)

        # ------------------------------------------------------------- 结论
        if method == "GET" and p == "/rconclusions":
            material_id = q("material_id")
            if not material_id:
                raise ValidationError("material_id 不能为空")
            return 200, {"items": _dataclass_list(service.list_conclusions(material_id))}
        if method == "POST" and p == "/rconclusions/supplement":
            r = service.supplement_authorization(actor_id=actor_id, **body)
            return 200 if r.replayed else 201, _receipt(r)
        if method == "POST" and p == "/rconclusions/revoke":
            r = service.revoke_authorization(actor_id=actor_id, **body)
            return 200 if r.replayed else 201, _receipt(r)
        if method == "POST" and p == "/rconclusions/withdraw":
            r = service.withdraw_authorization(actor_id=actor_id, **body)
            return 200 if r.replayed else 201, _receipt(r)
        if method == "POST" and p == "/rconclusions/sweep-expired":
            return 200, service.sweep_expired_authorizations(
                actor_id=actor_id, at=body.get("at"))
        if method == "GET" and p == "/rtrace":
            return 200, service.trace_conclusion(
                actor_id=actor_id, conclusion_id=q("conclusion_id") or None,
                material_id=q("material_id") or None)
        if method == "GET" and p == "/impact":
            material_id = q("material_id")
            if not material_id:
                raise ValidationError("material_id 不能为空")
            return 200, service.impact_report(actor_id=actor_id, material_id=material_id)

        # ------------------------------------------------------ 版本/评审
        if method == "POST" and p == "/rwork-versions":
            r = service.create_work_version(actor_id=actor_id, **body)
            return 200 if r.replayed else 201, _receipt(r)
        if method == "POST" and p == "/rreviews/enter":
            r = service.enter_review(actor_id=actor_id, **body)
            return 200 if r.replayed else 201, _receipt(r)
        if method == "GET" and p == "/rreviews":
            work_id = q("work_id")
            if not work_id:
                raise ValidationError("work_id 不能为空")
            return 200, {"items": _dataclass_list(service.list_reviews(work_id))}
        if method == "GET" and p == "/review-pack":
            work_id = q("work_id")
            if not work_id:
                raise ValidationError("work_id 不能为空")
            return 200, service.review_pack(actor_id=actor_id, work_id=work_id)

        # --------------------------------------------------------- 商业合作
        if method == "POST" and p == "/rdeals":
            r = service.register_deal(actor_id=actor_id, **body)
            return 200 if r.replayed else 201, _receipt(r)
        if method == "GET" and p == "/rdeals":
            work_id = q("work_id")
            if not work_id:
                raise ValidationError("work_id 不能为空")
            return 200, {"items": _dataclass_list(service.list_deals(work_id))}
        if method == "POST" and p.startswith("/rdeals/") and p.endswith("/sign"):
            deal_id = p.split("/")[2]
            r = service.sign_deal(actor_id=actor_id, deal_id=deal_id)
            return 200 if r.replayed else 201, _receipt(r)
        if method == "POST" and p.startswith("/rdeals/") and p.endswith("/block"):
            deal_id = p.split("/")[2]
            r = service.block_deal(actor_id=actor_id, deal_id=deal_id,
                                   reason=body.get("reason", ""))
            return 200 if r.replayed else 201, _receipt(r)

        # ------------------------------------------------------------- 异议
        if method == "POST" and p == "/rdisputes":
            r = service.file_dispute(actor_id=actor_id, **body)
            return 200 if r.replayed else 201, _receipt(r)
        if method == "GET" and p.startswith("/rdisputes/"):
            dispute_id = p.rsplit("/", 1)[1]
            payload = service.get_dispute(dispute_id).__dict__
            payload["preservations"] = service.list_preservations(dispute_id)
            payload["freezes"] = service.list_freezes(dispute_id)
            return 200, payload
        if method == "POST" and p.startswith("/rdisputes/") and p.endswith("/preserve"):
            dispute_id = p.split("/")[2]
            r = service.preserve_evidence(actor_id=actor_id, dispute_id=dispute_id, **body)
            return 200 if r.replayed else 201, _receipt(r)
        if method == "POST" and p.startswith("/rdisputes/") and p.endswith("/freeze"):
            dispute_id = p.split("/")[2]
            r = service.partial_freeze(actor_id=actor_id, dispute_id=dispute_id, **body)
            return 200 if r.replayed else 201, _receipt(r)
        if method == "POST" and p.startswith("/rdisputes/") and p.endswith("/resolve"):
            dispute_id = p.split("/")[2]
            r = service.resolve_dispute(actor_id=actor_id, dispute_id=dispute_id, **body)
            return 200 if r.replayed else 201, _receipt(r)
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}
    return None


def route(service: RightsService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """权利链路由优先，未命中再回落到基础服务路由。"""

    headers = headers or {}
    body = body or {}
    result = rights_route(service, method, path, body, headers)
    if result is not None:
        return result
    return base_api.route(service, method, path, body, headers)


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为权利链路由调用。"""

    service: RightsService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动文化素材权利链核验服务。"""

    parser = argparse.ArgumentParser(description="启动文化素材权利链核验服务")
    parser.add_argument("--database", default="rights.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database, schema_extra=RIGHTS_SCHEMA)
    Handler.service = RightsService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
