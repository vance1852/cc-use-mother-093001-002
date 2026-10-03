"""权利链核验的 HTTP/JSON 边界，与基础服务路由组合对外提供接口。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from creative_program_foundation import api as foundation_api
from creative_program_foundation.errors import DomainError
from creative_program_foundation.service import DomainService

from .service import RightsChainService
from .storage import RightsDatabase


def route(rights: RightsChainService, foundation: DomainService, method: str, path: str,
          body: dict[str, Any] | None, headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到权利链服务或基础服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    segments = [segment for segment in parsed.path.split("/") if segment]
    if not segments or segments[0] != "rights":
        return foundation_api.route(foundation, method, path, body, headers)
    actor_id = headers.get("X-Actor-Id", "")
    try:
        return _rights_route(rights, method, segments, parse_qs(parsed.query), body, actor_id)
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _created(result: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return (200 if result.get("replayed") else 201), result


def _rights_route(rights: RightsChainService, method: str, segments: list[str],
                  query: dict[str, list[str]], body: dict[str, Any],
                  actor_id: str) -> tuple[int, dict[str, Any]]:
    head = segments[1] if len(segments) > 1 else ""
    rest = segments[2:]

    if head == "materials":
        if method == "POST" and not rest:
            return _created(rights.register_material(actor_id=actor_id, **body))
        if method == "GET" and len(rest) == 1:
            return 200, rights.get_material(actor_id, rest[0])
        if method == "POST" and len(rest) == 2 and rest[1] == "evidence":
            return _created(rights.upload_evidence(actor_id=actor_id, material_id=rest[0], **body))
        if method == "POST" and len(rest) == 2 and rest[1] == "licenses":
            return _created(rights.submit_license(actor_id=actor_id, material_id=rest[0], **body))

    if head == "licenses":
        if method == "POST" and rest == ["expire"]:
            return _created(rights.expire_licenses(actor_id=actor_id, **body))
        if method == "GET" and len(rest) == 2 and rest[1] == "impact":
            return 200, rights.license_impact(actor_id, rest[0])
        if method == "POST" and len(rest) == 2 and rest[1] == "revoke":
            return _created(rights.revoke_license(actor_id=actor_id, license_id=rest[0], **body))

    if head == "verification-tasks":
        if method == "GET" and not rest:
            status = query.get("status", ["pending"])[0]
            task_type = query.get("task_type", [None])[0]
            return 200, {"items": rights.list_verification_tasks(actor_id, status=status,
                                                                 task_type=task_type)}
        if method == "POST" and len(rest) == 2 and rest[1] == "decide":
            return _created(rights.decide_verification(actor_id=actor_id, task_id=rest[0], **body))

    if head == "works":
        if method == "POST" and not rest:
            return _created(rights.register_work(actor_id=actor_id, **body))
        if method == "GET" and len(rest) == 1:
            return 200, rights.get_work(actor_id, rest[0])
        if method == "POST" and len(rest) == 2 and rest[1] == "representative":
            return _created(rights.set_team_representative(actor_id=actor_id, work_id=rest[0], **body))
        if method == "POST" and len(rest) == 2 and rest[1] == "versions":
            return _created(rights.create_version(actor_id=actor_id, work_id=rest[0], **body))

    if head == "versions":
        if method == "GET" and len(rest) == 1:
            return 200, rights.get_version(actor_id, rest[0])
        if method == "POST" and len(rest) == 2 and rest[1] == "enter-review":
            return _created(rights.enter_review(actor_id=actor_id, version_id=rest[0], **body))
        if method == "POST" and len(rest) == 2 and rest[1] == "advance-stage":
            return _created(rights.advance_stage(actor_id=actor_id, version_id=rest[0], **body))
        if method == "GET" and len(rest) == 2 and rest[1] == "conclusions":
            return 200, {"items": rights.list_conclusions(actor_id, rest[0])}

    if head == "conclusions":
        if method == "GET" and len(rest) == 1:
            return 200, rights.get_conclusion(actor_id, rest[0])
        if method == "GET" and len(rest) == 2 and rest[1] == "basis":
            return 200, rights.trace_conclusion_basis(actor_id, rest[0])

    if head == "objections":
        if method == "POST" and not rest:
            return _created(rights.file_objection(actor_id=actor_id, **body))
        if method == "GET" and not rest:
            status = query.get("status", [None])[0]
            return 200, {"items": rights.list_objections(actor_id, status=status)}
        if method == "GET" and len(rest) == 1:
            return 200, rights.get_objection(actor_id, rest[0])
        if method == "POST" and len(rest) == 2 and rest[1] == "preserve-evidence":
            return _created(rights.preserve_evidence(actor_id=actor_id, objection_id=rest[0], **body))
        if method == "POST" and len(rest) == 2 and rest[1] == "freeze":
            return _created(rights.freeze_targets(actor_id=actor_id, objection_id=rest[0], **body))
        if method == "POST" and len(rest) == 2 and rest[1] == "settle":
            return _created(rights.settle_objection(actor_id=actor_id, objection_id=rest[0], **body))
        if method == "POST" and len(rest) == 2 and rest[1] == "adjudicate":
            return _created(rights.adjudicate_objection(actor_id=actor_id, objection_id=rest[0], **body))

    if head == "collaborations":
        if method == "POST" and not rest:
            return _created(rights.create_collaboration(actor_id=actor_id, **body))
        if method == "GET" and not rest:
            version_id = query.get("version_id", [None])[0]
            return 200, {"items": rights.list_collaborations(actor_id, version_id=version_id)}
        if method == "POST" and len(rest) == 2 and rest[1] == "sign":
            return _created(rights.sign_collaboration(actor_id=actor_id,
                                                      collaboration_id=rest[0], **body))

    if head == "recovery-check" and method == "GET" and not rest:
        return 200, rights.verify_recovery(actor_id)

    return 404, {"error": "route_not_found", "message": "接口不存在"}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为组合路由调用。"""

    rights: RightsChainService
    foundation: DomainService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.rights, self.foundation, self.command, self.path, body,
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
    """启动权利链核验 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动文化素材权利链核验服务")
    parser.add_argument("--database", default="rights_chain.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = RightsDatabase(args.database)
    Handler.foundation = DomainService(database)
    Handler.rights = RightsChainService(database)
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
