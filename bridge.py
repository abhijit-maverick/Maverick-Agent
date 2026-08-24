#!/usr/bin/env python3
"""Maverick Pro local AI bridge.

Runs on loopback only, serves Maverick_Pro_Agent.html, and invokes a local terminal agent
(Codex CLI by default; Claude CLI or a custom command can be selected with env vars).
No Python packages are required.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shlex
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
HTML_FILE = ROOT / "Maverick_Pro_Agent.html"
SESSIONS = ROOT / "sessions"
RESULTS = ROOT / "results"
SESSIONS.mkdir(exist_ok=True)
RESULTS.mkdir(exist_ok=True)

HOST = os.environ.get("MAVERICK_HOST", "127.0.0.1")
PORT = int(os.environ.get("MAVERICK_PORT", "8765"))
AGENT = os.environ.get("MAVERICK_AGENT", "codex").strip().lower()
TIMEOUT = int(os.environ.get("MAVERICK_AGENT_TIMEOUT", "420"))
CUSTOM_CMD = os.environ.get("MAVERICK_AGENT_CMD", "").strip()

LOCK = threading.Lock()


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def safe_id(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", value or "session")
    return value[:120] or "session"


def session_path(session_id: str) -> Path:
    return SESSIONS / f"{safe_id(session_id)}.json"


def result_path(session_id: str) -> Path:
    return RESULTS / f"{safe_id(session_id)}_final.json"


def load_session(session_id: str) -> dict:
    p = session_path(session_id)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "schema_version": "1.0",
        "session_id": session_id,
        "created_at": now_iso(),
        "direction": None,
        "total_batches": None,
        "batches": {},
    }


def save_session(session: dict) -> None:
    session["updated_at"] = now_iso()
    p = session_path(session["session_id"])
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(session, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def agent_command() -> list[str]:
    if CUSTOM_CMD:
        return shlex.split(CUSTOM_CMD, posix=(os.name != "nt"))
    if AGENT == "codex":
        # Prompt is sent on stdin. Override with MAVERICK_AGENT_CMD if your Codex CLI
        # version uses different flags.
        return ["codex", "exec", "--skip-git-repo-check", "--sandbox", "read-only", "-"]
    if AGENT == "claude":
        # Claude Code non-interactive/print mode; override if needed.
        return ["claude", "-p", "--output-format", "text"]
    if AGENT == "mock":
        return ["<mock>"]
    raise RuntimeError(f"Unsupported MAVERICK_AGENT={AGENT!r}")


def run_agent(prompt: str) -> str:
    if AGENT == "mock":
        return mock_agent(prompt)
    cmd = agent_command()
    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=TIMEOUT,
            cwd=str(ROOT),
            shell=False,
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            f"Agent command not found: {cmd[0]!r}. Install/sign in to the CLI, or set MAVERICK_AGENT_CMD."
        ) from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"Agent timed out after {TIMEOUT}s") from e

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"Agent exited with code {proc.returncode}: {err[-1800:]}")
    out = (proc.stdout or "").strip()
    if not out:
        out = (proc.stderr or "").strip()
    if not out:
        raise RuntimeError("Agent returned no output")
    return out


def mock_agent(prompt: str) -> str:
    # Development-only mode used to verify the HTML/bridge workflow without an agent.
    m = re.search(r"MAVERICK_BATCH_INPUT\s*(\{.*?\})\s*END_MAVERICK_BATCH_INPUT", prompt, re.S)
    if m:
        data = json.loads(m.group(1))
        batch = data.get("batch", {})
        rows = []
        for c in data.get("candidates", []):
            rows.append({
                "symbol": c.get("symbol"),
                "verdict": "WATCH",
                "trend_status": "ESTABLISHED",
                "entry_status": "WAIT FOR RETEST",
                "size_multiplier": None,
                "evidence": [{"field": "mock", "value": "test", "reason": "Bridge test only"}],
                "main_risk": "MOCK MODE — no live underwriting performed",
                "flip_condition": "Run with Codex or Claude",
            })
        return json.dumps({
            "type": "maverick_batch_result",
            "batch_number": batch.get("number"),
            "total_batches": batch.get("total"),
            "results": rows,
        })
    # Finalizer mock
    return json.dumps({"type": "maverick_final_decision", "finalists": [], "best_candidate": None, "summary": "MOCK MODE"})


def extract_json(text: str) -> dict:
    text = text.strip()
    # Prefer a fenced JSON block.
    for m in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S | re.I):
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # Direct JSON.
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    # Balanced first JSON object, respecting quoted strings.
    starts = [i for i, ch in enumerate(text) if ch == "{"]
    for start in starts:
        depth = 0
        ins = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if ins:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    ins = False
            else:
                if ch == '"':
                    ins = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            obj = json.loads(text[start:i+1])
                            if isinstance(obj, dict):
                                return obj
                        except Exception:
                            break
    raise RuntimeError("Could not extract a valid JSON object from agent output")


def normalize_batch_result(obj: dict, expected_symbols: list[str], batch_number: int, total_batches: int) -> dict:
    rows = obj.get("results")
    if not isinstance(rows, list):
        raise RuntimeError("Agent JSON is missing results[]")
    expected = {str(s).upper() for s in expected_symbols}
    by_symbol = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol", "")).upper().strip()
        if sym not in expected:
            continue
        verdict = str(row.get("verdict", "WATCH")).upper()
        if verdict not in {"TAKE", "WATCH", "SKIP"}:
            verdict = "WATCH"
        trend = str(row.get("trend_status", "DEVELOPING")).upper()
        if trend not in {"ESTABLISHED", "DEVELOPING", "LOST"}:
            trend = "DEVELOPING"
        entry = str(row.get("entry_status", "")).upper().replace("_", " ")
        size = row.get("size_multiplier")
        if verdict != "TAKE":
            size = None
        elif size not in (1, 1.0, 0.5, 0.25):
            size = 0.25
        by_symbol[sym] = {
            "symbol": sym,
            "verdict": verdict,
            "trend_status": trend,
            "entry_status": entry or "NOT SUPPLIED",
            "size_multiplier": size,
            "evidence": row.get("evidence") if isinstance(row.get("evidence"), list) else [],
            "main_risk": str(row.get("main_risk", "NOT SUPPLIED")),
            "flip_condition": str(row.get("flip_condition", "NOT SUPPLIED")),
        }
    missing = [s for s in expected_symbols if s.upper() not in by_symbol]
    if missing:
        raise RuntimeError("Agent omitted stocks from the batch: " + ", ".join(missing))
    return {
        "schema_version": "1.0",
        "type": "maverick_batch_result",
        "batch_number": batch_number,
        "total_batches": total_batches,
        "results": [by_symbol[s.upper()] for s in expected_symbols],
    }


def make_batch_prompt(req: dict) -> str:
    payload = req["payload"]
    schema = {
        "type": "maverick_batch_result",
        "batch_number": req["batch_number"],
        "total_batches": req["total_batches"],
        "results": [{
            "symbol": "EXACT_SYMBOL",
            "verdict": "TAKE|WATCH|SKIP",
            "trend_status": "ESTABLISHED|DEVELOPING|LOST",
            "entry_status": "READY NOW|WAIT FOR PULLBACK-RETEST|WAIT FOR BOUNCE-RETEST|WAIT FOR BREAKOUT|WAIT FOR BREAKDOWN|CHASING",
            "size_multiplier": "1.0|0.5|0.25|null",
            "evidence": [{"field": "field.name", "value": "supplied value", "reason": "brief interpretation"}],
            "main_risk": "one concise line",
            "flip_condition": "one observable condition",
        }]
    }
    return (
        req.get("underwriting_prompt", "")
        + "\n\n## LOCAL AGENT EXECUTION CONTRACT\n"
        + "You are processing one Maverick batch. Judge only the stocks supplied in this batch. "
          "Do not invent additional symbols and do not compare them globally. "
          "If you have a browser/web tool, use it only for the requested live risk checks. "
          "If you do not have such a tool or cannot verify a live field, mark it UNKNOWN/NOT SUPPLIED and cap at WATCH.\n"
        + "Return ONLY one valid JSON object. No markdown fences and no prose outside JSON.\n"
        + "Required JSON shape:\n" + json.dumps(schema, indent=2)
        + "\n\nMAVERICK_BATCH_INPUT\n" + json.dumps(payload, ensure_ascii=False)
        + "\nEND_MAVERICK_BATCH_INPUT\n"
    )


def build_final_prompt(req: dict, session: dict) -> str:
    all_results = []
    candidates = []
    for k in sorted(session["batches"], key=lambda x: int(x)):
        b = session["batches"][k]
        all_results.extend(b["result"]["results"])
        candidates.extend(b.get("candidates", []))
    survivors = {r["symbol"] for r in all_results if r["verdict"] in {"TAKE", "WATCH"}}
    survivor_data = [c for c in candidates if str(c.get("symbol", "")).upper() in survivors]
    schema = {
        "type": "maverick_final_decision",
        "finalists": [{
            "rank": 1,
            "symbol": "SYMBOL",
            "verdict": "TAKE|WATCH",
            "entry_status": "current entry status",
            "size_multiplier": "1.0|0.5|0.25|null",
            "reason": "why this already-existing trend is the strongest participation candidate",
            "initial_stop": "supplied 1h structural stop or NOT SUPPLIED",
            "partial_level": "supplied structural level or NOT SUPPLIED",
            "trail": "structural trail",
            "invalidation": "single observable invalidation",
            "external_risk": "verified risk or UNKNOWN",
        }],
        "best_candidate": {
            "symbol": "SYMBOL",
            "verdict": "TAKE",
            "reason": "concise reason",
            "invalidation": "single observable invalidation"
        },
        "summary": "one concise sentence"
    }
    return (
        req.get("finalist_prompt", "")
        + "\n\n## FINAL AGENT CONTRACT\n"
        + "Batch boundaries are now irrelevant. Compare the surviving TAKE/WATCH names globally. "
          "Do not forecast price. Select only an already-established, intact trend with an actionable non-chased entry. "
          "If no TAKE is genuinely actionable now, best_candidate MUST be null. "
          "Return ONLY JSON; no markdown or prose outside JSON.\n"
        + "Required JSON shape:\n" + json.dumps(schema, indent=2)
        + "\n\nALL_BATCH_VERDICTS\n" + json.dumps(all_results, ensure_ascii=False)
        + "\nSURVIVOR_TECHNICAL_DATA\n" + json.dumps(survivor_data, ensure_ascii=False)
        + "\nEND_FINAL_INPUT\n"
    )


def normalize_final_decision(obj: dict, session: dict) -> dict:
    all_results = []
    for k in sorted(session["batches"], key=lambda x: int(x)):
        all_results.extend(session["batches"][k]["result"]["results"])
    allowed = {r["symbol"] for r in all_results if r["verdict"] in {"TAKE", "WATCH"}}
    result_map = {r["symbol"]: r for r in all_results}
    finalists = []
    seen = set()
    for raw in obj.get("finalists", []) if isinstance(obj.get("finalists"), list) else []:
        if not isinstance(raw, dict):
            continue
        sym = str(raw.get("symbol", "")).upper().strip()
        if sym not in allowed or sym in seen:
            continue
        seen.add(sym)
        base = result_map[sym]
        finalists.append({
            "rank": len(finalists) + 1,
            "symbol": sym,
            "verdict": base["verdict"],
            "entry_status": str(raw.get("entry_status", base.get("entry_status", ""))),
            "size_multiplier": raw.get("size_multiplier", base.get("size_multiplier")),
            "reason": str(raw.get("reason", "")),
            "initial_stop": raw.get("initial_stop", "NOT SUPPLIED"),
            "partial_level": raw.get("partial_level", "NOT SUPPLIED"),
            "trail": str(raw.get("trail", "")),
            "invalidation": str(raw.get("invalidation", base.get("flip_condition", ""))),
            "external_risk": str(raw.get("external_risk", base.get("main_risk", "UNKNOWN"))),
        })
    best_raw = obj.get("best_candidate")
    best = None
    if isinstance(best_raw, dict):
        sym = str(best_raw.get("symbol", "")).upper().strip()
        # A best actionable candidate must have been a TAKE in batch underwriting.
        if sym in result_map and result_map[sym]["verdict"] == "TAKE":
            best = {
                "symbol": sym,
                "verdict": "TAKE",
                "reason": str(best_raw.get("reason", "")),
                "invalidation": str(best_raw.get("invalidation", result_map[sym].get("flip_condition", ""))),
            }
    llm_cleared = [r for r in all_results if r["verdict"] == "TAKE"]
    watches = [r for r in all_results if r["verdict"] == "WATCH"]
    return {
        "schema_version": "1.0",
        "type": "maverick_final_result",
        "session_id": session["session_id"],
        "direction": session.get("direction"),
        "generated_at": now_iso(),
        "stocks_evaluated": len(all_results),
        "results": all_results,
        "llm_cleared": llm_cleared,
        "watch_candidates": watches,
        "finalists": finalists,
        "best_candidate": best,
        "summary": str(obj.get("summary", "")),
    }


def process_underwrite(req: dict) -> dict:
    required = ["session_id", "batch_number", "total_batches", "payload"]
    for k in required:
        if k not in req:
            raise RuntimeError(f"Missing request field: {k}")
    sid = str(req["session_id"])
    bn = int(req["batch_number"])
    total = int(req["total_batches"])
    payload = req["payload"]
    candidates = payload.get("candidates", []) if isinstance(payload, dict) else []
    symbols = [str(c.get("symbol", "")).upper() for c in candidates if isinstance(c, dict) and c.get("symbol")]
    if not symbols:
        raise RuntimeError("No candidates in payload")

    # Run agent outside lock so one long request does not block health checks.
    raw = run_agent(make_batch_prompt(req))
    batch_obj = extract_json(raw)
    batch_result = normalize_batch_result(batch_obj, symbols, bn, total)

    with LOCK:
        session = load_session(sid)
        session["direction"] = req.get("direction")
        session["total_batches"] = total
        session["batches"][str(bn)] = {
            "saved_at": now_iso(),
            "symbols": symbols,
            "candidates": candidates,
            "result": batch_result,
        }
        save_session(session)

    final_result = None
    is_final = bool(req.get("is_final_batch")) or bn == total
    if is_final:
        with LOCK:
            session = load_session(sid)
        missing_batches = [i for i in range(1, total + 1) if str(i) not in session.get("batches", {})]
        if missing_batches:
            raise RuntimeError("Cannot finalize; missing batch(es): " + ", ".join(map(str, missing_batches)))
        final_raw = run_agent(build_final_prompt(req, session))
        final_obj = extract_json(final_raw)
        final_result = normalize_final_decision(final_obj, session)
        result_path(sid).write_text(json.dumps(final_result, indent=2, ensure_ascii=False), encoding="utf-8")
        with LOCK:
            session = load_session(sid)
            session["final_result"] = final_result
            save_session(session)

    return {"ok": True, "batch_result": batch_result, "final_result": final_result}


class Handler(BaseHTTPRequestHandler):
    server_version = "MaverickBridge/1.0"

    def log_message(self, fmt, *args):
        sys.stdout.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))
        sys.stdout.flush()

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        if origin == "null":
            return True
        try:
            u = urlparse(origin)
            return u.hostname in {"127.0.0.1", "localhost", "::1"}
        except Exception:
            return False

    def _cors(self):
        origin = self.headers.get("Origin")
        if origin and self._origin_ok():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, status: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        if not self._origin_ok():
            return self._json(403, {"ok": False, "error": "Origin not allowed"})
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path in {"/", "/Maverick_Pro_Agent.html"}:
            if not HTML_FILE.exists():
                return self._json(404, {"ok": False, "error": f"Missing {HTML_FILE.name}"})
            body = HTML_FILE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/health":
            try:
                cmd = agent_command()
                cmd_display = " ".join(cmd) if AGENT != "mock" else "mock"
                self._json(200, {
                    "ok": True,
                    "agent": AGENT,
                    "command": cmd_display,
                    "mock": AGENT == "mock",
                    "host": HOST,
                    "port": PORT,
                    "html": HTML_FILE.name,
                })
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
            return
        self._json(404, {"ok": False, "error": "Not found"})

    def do_POST(self):
        if not self._origin_ok():
            return self._json(403, {"ok": False, "error": "Origin not allowed"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n > 5_000_000:
                raise RuntimeError("Request too large")
            data = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
            if self.path == "/api/underwrite":
                print(f"\n>>> Session {data.get('session_id')} · Batch {data.get('batch_number')}/{data.get('total_batches')} · agent={AGENT}", flush=True)
                result = process_underwrite(data)
                print(f"<<< Batch {data.get('batch_number')} complete" + (" · FINAL READY" if result.get("final_result") else ""), flush=True)
                return self._json(200, result)
            if self.path == "/api/reset":
                sid = str(data.get("session_id", ""))
                if sid:
                    p = session_path(sid)
                    if p.exists():
                        p.unlink()
                    rp = result_path(sid)
                    if rp.exists():
                        rp.unlink()
                return self._json(200, {"ok": True})
            return self._json(404, {"ok": False, "error": "Not found"})
        except Exception as e:
            print("!!!", type(e).__name__, str(e), flush=True)
            return self._json(500, {"ok": False, "error": str(e)})


def main():
    if not HTML_FILE.exists():
        print(f"ERROR: Put {HTML_FILE.name} beside bridge.py", file=sys.stderr)
        raise SystemExit(2)
    print("Maverick Pro Local Agent Bridge")
    print("-" * 36)
    print(f"App:   http://{HOST}:{PORT}")
    print(f"Agent: {AGENT}")
    try:
        print("Cmd:  ", " ".join(agent_command()))
    except Exception as e:
        print("Cmd:   ERROR:", e)
    print("\nKeep this terminal open while using Maverick. Ctrl+C stops the bridge.\n")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Maverick bridge…")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
