import hashlib
import hmac
import json
import os
import re
import secrets
import time
import uuid

import requests
from flask import Flask, jsonify, render_template, request


app = Flask(__name__)

COMMAND_TTL_SECONDS = 600
SESSION_TTL_SECONDS = 60 * 60 * 12
SESSION_COOKIE_NAME = "pc_remote_session"
SESSION_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
PAIRING_CODE_TTL_SECONDS = 300
PAIRING_ATTEMPTS_PER_MINUTE = 5
PAIRING_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ123456789"
PAIRING_CODE_PATTERN = re.compile(r"^[A-HJ-NP-Z1-9]{3}$")
COMMAND_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
ALLOWED_ACTIONS = {"screenshot", "list_apps", "launch_app", "close_app"}


class ConfigurationError(RuntimeError):
    pass


def redis_command(*parts):
    redis_url = os.environ.get("UPSTASH_REDIS_REST_URL")
    redis_token = os.environ.get("UPSTASH_REDIS_REST_TOKEN")
    missing_variables = [
        name
        for name, value in (
            ("UPSTASH_REDIS_REST_URL", redis_url),
            ("UPSTASH_REDIS_REST_TOKEN", redis_token),
        )
        if not value
    ]
    if missing_variables:
        raise ConfigurationError(
            f"Missing environment variables: {', '.join(missing_variables)}"
        )

    response = requests.post(
        redis_url.rstrip("/"),
        headers={"Authorization": f"Bearer {redis_token}"},
        json=list(parts),
        timeout=5,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError(payload["error"])
    return payload.get("result")


def redis_json(key):
    value = redis_command("GET", key)
    return json.loads(value) if value else None


def agent_is_configured():
    return bool(os.environ.get("AGENT_TOKEN"))


def require_login():
    session_token = request.cookies.get(SESSION_COOKIE_NAME, "")
    if not SESSION_TOKEN_PATTERN.fullmatch(session_token):
        return jsonify(error="Authentication required"), 401
    if not redis_command("GET", f"browser:session:{session_token}"):
        return jsonify(error="Authentication required"), 401
    return None


def require_agent():
    expected = os.environ.get("AGENT_TOKEN", "")
    supplied = request.headers.get("Authorization", "")
    supplied = supplied.removeprefix("Bearer ")
    if not expected or not hmac.compare_digest(supplied, expected):
        return jsonify(error="Agent authentication failed"), 401
    return None


def pairing_attempts_allowed():
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    client_address = forwarded_for.split(",", 1)[0].strip() or request.remote_addr or "unknown"
    address_hash = hashlib.sha256(client_address.encode("utf-8")).hexdigest()[:24]
    key = f"pairing:attempts:{address_hash}"
    attempts = redis_command("INCR", key)
    if attempts == 1:
        redis_command("EXPIRE", key, 60)
    return attempts <= PAIRING_ATTEMPTS_PER_MINUTE


@app.errorhandler(ConfigurationError)
def handle_configuration_error(error):
    return jsonify(error=str(error)), 503


@app.errorhandler(requests.RequestException)
def handle_redis_error(error):
    app.logger.error("Redis request failed: %s", error)
    return jsonify(error="The command service is temporarily unavailable"), 503


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/login")
def login():
    supplied = request.get_json(silent=True)
    if not isinstance(supplied, dict):
        return jsonify(error="Invalid login request"), 400
    if not pairing_attempts_allowed():
        return jsonify(error="Too many attempts; try again in a minute"), 429

    code = str(supplied.get("code", "")).strip().upper()
    if not PAIRING_CODE_PATTERN.fullmatch(code):
        return jsonify(error="Invalid or expired pairing code"), 401
    active_code = redis_command("GET", "agent:pairing_code") or ""
    if not hmac.compare_digest(code, active_code):
        return jsonify(error="Invalid or expired pairing code"), 401

    session_token = secrets.token_urlsafe(32)
    redis_command(
        "SET",
        f"browser:session:{session_token}",
        "1",
        "EX",
        SESSION_TTL_SECONDS,
    )
    response = jsonify(ok=True)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=True,
        samesite="Strict",
        path="/",
    )
    return response


@app.post("/api/agent/register")
def agent_register():
    denied = require_agent()
    if denied:
        return denied

    code = "".join(secrets.choice(PAIRING_CODE_ALPHABET) for _ in range(3))
    redis_command(
        "SET",
        "agent:pairing_code",
        code,
        "EX",
        PAIRING_CODE_TTL_SECONDS,
    )
    return jsonify(code=code, expires_in=PAIRING_CODE_TTL_SECONDS)


@app.post("/api/logout")
def logout():
    session_token = request.cookies.get(SESSION_COOKIE_NAME, "")
    if SESSION_TOKEN_PATTERN.fullmatch(session_token):
        redis_command("DEL", f"browser:session:{session_token}")
    response = jsonify(ok=True)
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        httponly=True,
        secure=True,
        samesite="Strict",
        path="/",
    )
    return response


@app.get("/api/status")
def status():
    denied = require_login()
    if denied:
        return denied

    last_seen = redis_command("GET", "agent:last_seen")
    app_names = redis_json("agent:apps") or []
    last_seen_at = float(last_seen) if last_seen else 0
    return jsonify(
        connected=(time.time() - last_seen_at) < 30,
        apps=app_names,
    )


@app.post("/api/commands")
def create_command():
    denied = require_login()
    if denied:
        return denied
    if not agent_is_configured():
        return jsonify(error="AGENT_TOKEN is not configured"), 503

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="Invalid command request"), 400
    action = body.get("action")
    if not isinstance(action, str) or action not in ALLOWED_ACTIONS:
        return jsonify(error="Unsupported action"), 400

    command = {"id": uuid.uuid4().hex, "action": action, "status": "queued"}
    if action in {"launch_app", "close_app"}:
        name = body.get("app")
        allowed_apps = redis_json("agent:apps") or []
        if not isinstance(name, str) or name not in allowed_apps:
            return jsonify(error="Choose an app from the agent's allowlist"), 400
        command["app"] = name

    encoded = json.dumps(command, separators=(",", ":"))
    redis_command("SET", f"command:{command['id']}", encoded, "EX", COMMAND_TTL_SECONDS)
    redis_command("RPUSH", "command:queue", command["id"])
    return jsonify(id=command["id"], status=command["status"]), 202


@app.get("/api/commands/<command_id>")
def get_command(command_id):
    denied = require_login()
    if denied:
        return denied
    if not COMMAND_ID_PATTERN.fullmatch(command_id):
        return jsonify(error="Unknown command"), 404

    command = redis_json(f"command:{command_id}")
    if not command:
        return jsonify(error="Command expired or not found"), 404
    return jsonify(command)


@app.post("/api/agent/next")
def agent_next():
    denied = require_agent()
    if denied:
        return denied

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="Invalid agent request"), 400
    apps = body.get("apps", [])
    if not isinstance(apps, list) or len(apps) > 100 or not all(
        isinstance(name, str) and len(name) <= 80 for name in apps
    ):
        return jsonify(error="Invalid app list"), 400

    redis_command("SET", "agent:last_seen", str(time.time()), "EX", 30)
    redis_command("SET", "agent:apps", json.dumps(apps), "EX", 30)
    command_id = redis_command("LPOP", "command:queue")
    if not command_id:
        return jsonify(command=None)

    command = redis_json(f"command:{command_id}")
    if not command:
        return jsonify(command=None)
    command["status"] = "running"
    redis_command(
        "SET",
        f"command:{command_id}",
        json.dumps(command, separators=(",", ":")),
        "EX",
        COMMAND_TTL_SECONDS,
    )
    return jsonify(command=command)


@app.post("/api/agent/results/<command_id>")
def agent_result(command_id):
    denied = require_agent()
    if denied:
        return denied
    if not COMMAND_ID_PATTERN.fullmatch(command_id):
        return jsonify(error="Unknown command"), 404

    command = redis_json(f"command:{command_id}")
    if not command:
        return jsonify(error="Command expired or not found"), 404
    result = request.get_json(silent=True)
    if not isinstance(result, dict):
        return jsonify(error="Invalid command result"), 400

    command["status"] = "done"
    command["result"] = result
    redis_command(
        "SET",
        f"command:{command_id}",
        json.dumps(command, separators=(",", ":")),
        "EX",
        COMMAND_TTL_SECONDS,
    )
    return jsonify(ok=True)