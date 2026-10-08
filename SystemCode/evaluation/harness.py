"""Runs the real system (FastAPI + Pi worker) and plays test cases against it over HTTP."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


class System:
    def __init__(self, api_port: int = 8790, pi_port: int = 8093, state_db: Path | None = None, env: dict | None = None):
        self.base = f"http://127.0.0.1:{api_port}/api/v1"
        self.pi_port = pi_port
        state_db = state_db or ROOT / "runtime" / f"eval_{uuid.uuid4().hex[:8]}.db"
        self.env = {**os.environ, "BUILDRIG_STATE_DB": str(state_db),
                    "BUILDRIG_PI_RUNTIME_URL": f"http://127.0.0.1:{pi_port}", **(env or {})}
        self.api = subprocess.Popen([sys.executable, "-m", "uvicorn", "main:app", "--port", str(api_port)], cwd=ROOT,
                                    env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.pi = subprocess.Popen(["node", "--env-file=../.env", "dist/server.js"], cwd=ROOT / "pi-worker",
                                   env={**self.env, "PI_PORT": str(pi_port), "BUILDRIG_API_URL": f"http://127.0.0.1:{api_port}"},
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._wait(f"{self.base}/health")
        self._wait(f"http://127.0.0.1:{pi_port}/health")

    @staticmethod
    def _wait(url: str):
        for _ in range(120):
            try:
                urlopen(url, timeout=2)
                return
            except Exception:
                time.sleep(1)
        raise RuntimeError(f"{url} did not come up")

    def call(self, method: str, path: str, body: dict | None = None, user: str = "eval"):
        req = Request(self.base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                      headers={"Content-Type": "application/json", "X-User-ID": user})
        try:
            return json.loads(urlopen(req, timeout=1800).read() or b"{}")
        except HTTPError as exc:
            return {"http_error": exc.code, "detail": json.loads(exc.read() or b"{}")}

    def say(self, session: str, text: str, version: int) -> dict:
        return self.call("POST", f"/sessions/{session}/messages",
                         {"client_message_id": uuid.uuid4().hex, "text": text, "expected_requirements_version": version})

    def recommend(self, session: str, version: int, mode: str = "dag", base: str | None = None, options: int = 1):
        started = time.time()
        body = {"requirements_version": version, "orchestration_mode": mode, "maximum_options": options}
        if base:
            body["base_run_id"] = base
        created = self.call("POST", f"/sessions/{session}/runs", body)
        if "id" not in created:
            return None, {"error": created}, time.time() - started
        while True:
            status = self.call("GET", f"/runs/{created['id']}")
            if status.get("status") in ("completed", "failed", "cancelled"):
                break
            time.sleep(2)
        result = self.call("GET", f"/runs/{created['id']}/result") if status["status"] == "completed" else {"error": status.get("error")}
        return created["id"], result, time.time() - started

    def play(self, case: dict, mode: str = "dag") -> dict:
        """All turns of a case; a recommendation is requested after each turn whose requirements are ready."""
        session = self.call("POST", "/sessions", {})["id"]
        version, last_run, record = 0, None, {"case_id": case["id"], "mode": mode, "turns": []}
        for text in case["turns"]:
            parsed = self.say(session, text, version)
            version = parsed["requirements_version"]
            turn = {"text": text, "requirements": parsed["requirements"], "questions": [q["question_id"] for q in parsed["questions"]],
                    "can_generate": parsed["can_generate"]}
            if parsed["can_generate"]:
                run_id, result, seconds = self.recommend(session, version, mode, base=last_run)
                turn.update(run_id=run_id, result=result, seconds=round(seconds, 1))
                if result.get("outcome"):
                    last_run = run_id
            record["turns"].append(turn)
        return record

    def close(self):
        self.pi.terminate()
        self.api.terminate()
