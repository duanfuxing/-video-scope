"""Persistent analysis steps and a shared, conservative USD budget ledger."""

import json
import os
import sqlite3
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

from video_scope.jobs import Store


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2, allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class AnalysisStore:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.root = store.root
        (self.root / "analyses").mkdir(exist_ok=True)
        (self.root / "campaigns").mkdir(exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS campaigns (
                    id TEXT PRIMARY KEY, budget_usd TEXT NOT NULL,
                    model TEXT NOT NULL, endpoint TEXT NOT NULL,
                    created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS analyses (
                    id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL REFERENCES campaigns(id),
                    cache_key TEXT NOT NULL, mode TEXT NOT NULL, source_hash TEXT NOT NULL,
                    state TEXT NOT NULL, error TEXT,
                    created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(campaign_id, cache_key)
                );
                CREATE TABLE IF NOT EXISTS supplier_calls (
                    id TEXT PRIMARY KEY, analysis_id TEXT NOT NULL REFERENCES analyses(id),
                    campaign_id TEXT NOT NULL REFERENCES campaigns(id), stage TEXT NOT NULL,
                    category TEXT NOT NULL, request_hash TEXT NOT NULL, state TEXT NOT NULL,
                    reserved_usd TEXT NOT NULL, estimated_usd TEXT, usage_json TEXT,
                    response_path TEXT NOT NULL, error TEXT,
                    created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(analysis_id, stage)
                );
            """)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.root / "tasks.sqlite3", timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def campaign(self, identifier: str, budget: Decimal, model: str, endpoint: str) -> Path:
        if not identifier.replace("-", "").isalnum():
            raise ValueError("campaign id must contain only letters, digits and hyphens")
        if not budget.is_finite() or not Decimal("0") < budget <= Decimal("5"):
            raise ValueError("authorized campaign budget must be in (0, 5] USD")
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO campaigns(id,budget_usd,model,endpoint) VALUES (?,?,?,?)",
                (identifier, str(budget), model, endpoint),
            )
            row = db.execute("SELECT * FROM campaigns WHERE id=?", (identifier,)).fetchone()
            assert row is not None
            if (Decimal(row["budget_usd"]), row["model"], row["endpoint"]) != (
                budget,
                model,
                endpoint,
            ):
                raise ValueError("existing campaign budget/model/endpoint cannot be reset")
        directory = self.root / "campaigns" / identifier
        directory.mkdir(exist_ok=True)
        return directory

    def analysis(self, campaign: str, cache_key: str, mode: str, source_hash: str) -> str:
        with self.connect() as db:
            row = db.execute(
                "SELECT id FROM analyses WHERE campaign_id=? AND cache_key=?",
                (campaign, cache_key),
            ).fetchone()
            if row:
                return str(row["id"])
            identifier = uuid.uuid4().hex
            db.execute(
                """INSERT INTO analyses(id,campaign_id,cache_key,mode,source_hash,state)
                   VALUES (?,?,?,?,?,'pending')""",
                (identifier, campaign, cache_key, mode, source_hash),
            )
        (self.root / "analyses" / identifier).mkdir(exist_ok=True)
        return identifier

    def set_state(self, analysis_id: str, state: str, error: str | None = None) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE analyses SET state=?,error=?,updated=CURRENT_TIMESTAMP WHERE id=?",
                (state, error, analysis_id),
            )

    def find_call(self, analysis_id: str, stage: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM supplier_calls WHERE analysis_id=? AND stage=?",
                (analysis_id, stage),
            ).fetchone()
        return dict(row) if row else None

    def reserve(
        self,
        analysis_id: str,
        stage: str,
        request_hash: str,
        amount: Decimal,
        category: str = "main",
    ) -> dict[str, Any]:
        if not amount.is_finite() or amount <= 0:
            raise ValueError("reservation must be a positive finite USD amount")
        if category not in {"main", "supplemental"}:
            raise ValueError("invalid call category")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            analysis = db.execute("SELECT * FROM analyses WHERE id=?", (analysis_id,)).fetchone()
            if analysis is None:
                raise ValueError("unknown analysis")
            campaign = db.execute(
                "SELECT * FROM campaigns WHERE id=?", (analysis["campaign_id"],)
            ).fetchone()
            assert campaign is not None
            calls = db.execute(
                "SELECT * FROM supplier_calls WHERE campaign_id=?", (campaign["id"],)
            ).fetchall()
            if any(call["state"] in {"sending", "unknown"} for call in calls):
                raise ValueError("campaign has an unresolved request; paid calls are paused")
            existing = next(
                (
                    call
                    for call in calls
                    if call["analysis_id"] == analysis_id and call["stage"] == stage
                ),
                None,
            )
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise ValueError("saved step request differs; refusing a silent replacement")
                return dict(existing)
            delivered_attempts = sum(call["state"] != "connection_failed" for call in calls)
            if delivered_attempts >= 12:
                raise ValueError("campaign call limit (12) reached")
            main_count = sum(
                call["analysis_id"] == analysis_id
                and call["category"] == "main"
                and call["state"] not in {"abandoned", "connection_failed", "rejected"}
                for call in calls
            )
            if category == "main" and main_count >= 2:
                raise ValueError("analysis main-call limit (2) reached")
            committed = sum(
                (
                    Decimal(
                        call["estimated_usd"]
                        if call["estimated_usd"] is not None
                        else call["reserved_usd"]
                    )
                    for call in calls
                    if call["state"] != "abandoned"
                ),
                Decimal("0"),
            )
            limit = Decimal(campaign["budget_usd"])
            if category == "main":
                limit = min(limit, Decimal("4"))
            if committed + amount > limit:
                raise ValueError(
                    "remaining authorized budget cannot cover this request reservation"
                )
            identifier = uuid.uuid4().hex
            response = f"analyses/{analysis_id}/{identifier}.response.json"
            db.execute(
                """INSERT INTO supplier_calls(
                       id,analysis_id,campaign_id,stage,category,request_hash,state,
                       reserved_usd,response_path) VALUES (?,?,?,?,?,?,'reserved',?,?)""",
                (
                    identifier,
                    analysis_id,
                    campaign["id"],
                    stage,
                    category,
                    request_hash,
                    str(amount),
                    response,
                ),
            )
        call = self.find_call(analysis_id, stage)
        assert call is not None
        return call

    def sending(self, call_id: str) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM supplier_calls WHERE id=?", (call_id,)).fetchone()
            if current is None or current["state"] != "reserved":
                raise ValueError("only an unsent reserved request can be dispatched")
            unresolved = db.execute(
                """SELECT COUNT(*) FROM supplier_calls WHERE campaign_id=?
                   AND state IN ('sending','unknown')""",
                (current["campaign_id"],),
            ).fetchone()[0]
            if unresolved:
                raise ValueError("another request is in flight or unknown")
            db.execute(
                "UPDATE supplier_calls SET state='sending',updated=CURRENT_TIMESTAMP WHERE id=?",
                (call_id,),
            )

    def unknown(self, call_id: str, error: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE supplier_calls SET state='unknown',error=?,"
                "updated=CURRENT_TIMESTAMP WHERE id=?",
                (error, call_id),
            )

    def finish(self, call_id: str, response: dict[str, Any]) -> None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM supplier_calls WHERE id=?", (call_id,)).fetchone()
        if row is None:
            raise ValueError("unknown call")
        if not (self.root / row["response_path"]).exists():
            raise ValueError("response must be durably saved before settlement")
        cost = Decimal(str(response["estimated_usd"]))
        if not cost.is_finite() or cost < 0:
            raise ValueError("invalid response cost")
        with self.connect() as db:
            db.execute(
                """UPDATE supplier_calls SET state='succeeded',estimated_usd=?,usage_json=?,
                   error=NULL,updated=CURRENT_TIMESTAMP WHERE id=?""",
                (str(cost), json.dumps(response["usage"], ensure_ascii=False), call_id),
            )
        if cost > Decimal(row["reserved_usd"]):
            self.unknown(call_id, "actual estimated cost exceeded reservation; reconcile billing")
            raise ValueError("supplier usage exceeded conservative reservation; stop and reconcile")

    def reject(self, call_id: str, *, reason: str) -> None:
        """Settle a confirmed, dispatched HTTP 400 rejection as non-billable."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("rejection reason must be a non-empty string")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM supplier_calls WHERE id=?", (call_id,)).fetchone()
            if current is None:
                raise ValueError("unknown call")
            if current["state"] == "rejected":
                if current["error"] != reason:
                    raise ValueError("rejected call reason cannot be changed")
                return
            if current["state"] != "unknown":
                raise ValueError("only an unknown call can be rejected")
            if current["estimated_usd"] is not None or current["usage_json"] is not None:
                raise ValueError("call with estimated usage cannot be rejected")

            response_path = self.root / current["response_path"]
            if response_path.is_file():
                raise ValueError("call with a durable response cannot be rejected")
            diagnostic_path = response_path.with_suffix(".error.json")
            try:
                diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError("valid error diagnostic is required for rejection") from exc
            if not isinstance(diagnostic, dict):
                raise ValueError("valid error diagnostic is required for rejection")
            if diagnostic.get("dispatched") is not True:
                raise ValueError("only a dispatched HTTP 400 can be rejected")
            http_code = diagnostic.get("http_code")
            if isinstance(http_code, bool) or not isinstance(http_code, int) or http_code != 400:
                raise ValueError("only a dispatched HTTP 400 can be rejected")

            db.execute(
                """UPDATE supplier_calls SET state='rejected',estimated_usd='0',
                   usage_json=NULL,error=?,updated=CURRENT_TIMESTAMP WHERE id=?""",
                (reason, call_id),
            )

    def abandon_unknown(self, call_id: str, *, source_hash: str, reason: str) -> dict[str, Any]:
        """Archive an unresolved request after an explicit user-authorized retry decision."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("abandon reason must be a non-empty string")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                """SELECT supplier_calls.*, analyses.source_hash
                   FROM supplier_calls
                   JOIN analyses ON analyses.id=supplier_calls.analysis_id
                   WHERE supplier_calls.id=?""",
                (call_id,),
            ).fetchone()
            if current is None:
                raise ValueError("unknown call")
            if current["source_hash"] != source_hash:
                raise ValueError("unknown call does not belong to the selected source")
            if current["state"] != "unknown":
                raise ValueError("only an unknown call can be abandoned for retry")
            if current["estimated_usd"] is not None or current["usage_json"] is not None:
                raise ValueError("call with estimated usage cannot be abandoned for retry")
            response_path = self.root / current["response_path"]
            if response_path.is_file():
                raise ValueError("call with a durable response cannot be abandoned for retry")

            archived_stage = f"{current['stage']}#abandoned-{call_id[:12]}"
            conflict = db.execute(
                "SELECT 1 FROM supplier_calls WHERE analysis_id=? AND stage=?",
                (current["analysis_id"], archived_stage),
            ).fetchone()
            if conflict is not None:
                raise ValueError("unknown call already has an archived retry stage")
            audit = json.dumps(
                {
                    "reason": reason.strip(),
                    "previous_error": current["error"],
                    "reservation_usd": current["reserved_usd"],
                    "billing_outcome": "unknown",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            db.execute(
                """UPDATE supplier_calls SET stage=?,state='abandoned',error=?,
                   updated=CURRENT_TIMESTAMP WHERE id=?""",
                (archived_stage, audit, call_id),
            )
        archived = self.find_call(str(current["analysis_id"]), archived_stage)
        assert archived is not None
        return archived

    def settle_connect_failure(self, call_id: str) -> dict[str, Any]:
        """Archive an HTTPX connection failure that occurred before HTTP delivery."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT * FROM supplier_calls WHERE id=?", (call_id,)
            ).fetchone()
            if current is None:
                raise ValueError("unknown call")
            if current["state"] == "connection_failed":
                return dict(current)
            if current["state"] != "unknown":
                raise ValueError("only an unknown call can be settled as a connection failure")
            if current["estimated_usd"] is not None or current["usage_json"] is not None:
                raise ValueError("call with estimated usage is not a connection failure")

            response_path = self.root / current["response_path"]
            if response_path.is_file():
                raise ValueError("call with a durable response is not a connection failure")
            diagnostic_path = response_path.with_suffix(".error.json")
            try:
                diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError("valid connection diagnostic is required") from exc
            progress = diagnostic.get("stream_progress")
            if (
                diagnostic.get("error_type") != "ConnectError"
                or diagnostic.get("http_code") is not None
                or diagnostic.get("dispatched") is not True
                or not isinstance(progress, dict)
                or progress.get("chunk_count") != 0
            ):
                raise ValueError("diagnostic is not a pre-delivery connection failure")
            stream_path = response_path.with_suffix(".stream.jsonl")
            if stream_path.is_file() and stream_path.stat().st_size != 0:
                raise ValueError("connection failure has received stream data")

            archived_stage = f"{current['stage']}#connect-failed-{call_id[:12]}"
            conflict = db.execute(
                "SELECT 1 FROM supplier_calls WHERE analysis_id=? AND stage=?",
                (current["analysis_id"], archived_stage),
            ).fetchone()
            if conflict is not None:
                raise ValueError("connection failure already has an archived stage")
            audit = json.dumps(
                {
                    "reason": "HTTPX ConnectError: failed to establish a connection",
                    "diagnostic_path": diagnostic_path.relative_to(self.root).as_posix(),
                    "billing_outcome": "not_delivered",
                },
                sort_keys=True,
            )
            db.execute(
                """UPDATE supplier_calls SET stage=?,state='connection_failed',
                   estimated_usd='0',usage_json=NULL,error=?,updated=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (archived_stage, audit, call_id),
            )
        archived = self.find_call(str(current["analysis_id"]), archived_stage)
        assert archived is not None
        return archived

    def recover(self, analysis_id: str) -> None:
        with self.connect() as db:
            calls = db.execute(
                "SELECT * FROM supplier_calls WHERE analysis_id=?", (analysis_id,)
            ).fetchall()
        for call in calls:
            if call["state"] not in {"sending", "unknown"}:
                continue
            path = self.root / call["response_path"]
            if path.is_file():
                response = json.loads(path.read_text(encoding="utf-8"))
                if response.get("estimated_usd") is not None and response.get("usage"):
                    self.finish(call["id"], response)
                    continue
            try:
                self.settle_connect_failure(call["id"])
                continue
            except ValueError:
                pass
            self.unknown(call["id"], "sent request has no durable billable response; do not resend")

    def summary(self, campaign_id: str) -> dict[str, Any]:
        with self.connect() as db:
            campaign = db.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
            if campaign is None:
                raise ValueError("unknown campaign")
            calls = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM supplier_calls WHERE campaign_id=? ORDER BY created,id",
                    (campaign_id,),
                )
            ]
            analyses = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM analyses WHERE campaign_id=? ORDER BY created,id", (campaign_id,)
                )
            ]
        estimated = sum(
            (Decimal(call["estimated_usd"]) for call in calls if call["estimated_usd"] is not None),
            Decimal("0"),
        )
        reserved = sum(
            (
                Decimal(call["reserved_usd"])
                for call in calls
                if call["estimated_usd"] is None
                and call["state"] in {"reserved", "sending", "unknown"}
            ),
            Decimal("0"),
        )
        abandoned = sum(
            (
                Decimal(call["reserved_usd"])
                for call in calls
                if call["state"] == "abandoned" and call["estimated_usd"] is None
            ),
            Decimal("0"),
        )
        return {
            "campaign_id": campaign_id,
            "budget_usd": campaign["budget_usd"],
            "estimated_usage_cost_usd": str(estimated),
            "unsettled_reserved_usd": str(reserved),
            "abandoned_reservation_usd": str(abandoned),
            "bill_is_final": False,
            "call_count": len(calls),
            "unknown_calls": sum(call["state"] in {"unknown", "sending"} for call in calls),
            "abandoned_calls": sum(call["state"] == "abandoned" for call in calls),
            "connection_failed_calls": sum(
                call["state"] == "connection_failed" for call in calls
            ),
            "analyses": analyses,
            "calls": [
                {key: value for key, value in call.items() if key != "usage_json"} for call in calls
            ],
        }
