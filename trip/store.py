"""Version trips in the same transaction as the execution request."""

import json

from app import Conflict, Store, TERMINAL
from workflow.trip_contract import WORKFLOW, normalize_revision, normalize_submission, normalize_trip


class TripStore(Store):
    validate_payload = staticmethod(normalize_submission)

    def __init__(self, data_dir):
        super().__init__(data_dir)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS trip_revisions (
                parent_run_id TEXT PRIMARY KEY, child_run_id TEXT NOT NULL UNIQUE)""")

    def revise_run(self, parent_run_id, body, key):
        changes, feedback = normalize_revision(body)
        digest = self.request_hash({"parent_run_id": parent_run_id, "changes": changes, "feedback": feedback}, key)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = self.existing_run(db, key, digest)
            if prior:
                return prior, False
            row = db.execute("SELECT state_json FROM runs WHERE run_id=?", (parent_run_id,)).fetchone()
            if not row:
                raise KeyError(parent_run_id)
            parent = json.loads(row[0])
            if parent["status"] not in TERMINAL:
                raise Conflict("请等待当前版本执行结束")
            if db.execute("SELECT 1 FROM trip_revisions WHERE parent_run_id=?", (parent_run_id,)).fetchone():
                raise Conflict("已有后续行程版本，请从最新版本修订")
            old_request = parent["request"]
            trip = normalize_trip({**old_request["trip"], **changes})
            if trip == old_request["trip"] and not feedback:
                raise ValueError("行程资料没有变化，请填写修订要求")
            payload = {
                "workflow": WORKFLOW, "trip": trip, "feedback": feedback,
                "trip_id": old_request.get("trip_id", parent_run_id),
                "parent_run_id": parent_run_id, "version": old_request["version"] + 1,
            }
            child = self.insert_run(db, payload, key, digest)
            db.execute("INSERT INTO trip_revisions VALUES (?,?)", (parent_run_id, child["run_id"]))
            return child, True
