import base64
import io
import logging
import os
import subprocess
import time

import mss
import requests
from PIL import Image


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOGGER = logging.getLogger("pc-client")

REMOTE_CONTROL_URL = "https://your-project.vercel.app"
AGENT_TOKEN = "paste-the-same-agent-token-configured-in-vercel"
ALLOWED_APPS = {
    "Calculator": ["calc.exe"],
}


def load_allowed_apps():
    configured = ALLOWED_APPS
    for name, command in configured.items():
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 80
            or not isinstance(command, list)
            or not command
            or not all(isinstance(part, str) and part and "\0" not in part for part in command)
        ):
            raise SystemExit("Each allowed app must map a name to a non-empty argument list")
    return configured


class PcAgent:
    def __init__(self, base_url, token, allowed_apps):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})
        self.allowed_apps = allowed_apps
        self.processes = {}

    def app_states(self):
        states = []
        for name in self.allowed_apps:
            process = self.processes.get(name)
            running = process is not None and process.poll() is None
            states.append({"name": name, "running": running})
            if process is not None and not running:
                self.processes.pop(name, None)
        return states

    def screenshot(self):
        with mss.mss() as capture:
            monitor = capture.monitors[1]
            frame = capture.grab(monitor)
            image = Image.frombytes("RGB", frame.size, frame.rgb)

        image.thumbnail((1280, 800))
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=65, optimize=True)
        while output.tell() > 450_000 and image.width > 640:
            image.thumbnail((max(640, image.width * 3 // 4), max(400, image.height * 3 // 4)))
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=50, optimize=True)
        return base64.b64encode(output.getvalue()).decode("ascii")

    def execute(self, command):
        action = command.get("action")
        if action == "screenshot":
            return {"ok": True, "image": self.screenshot()}
        if action == "list_apps":
            return {"ok": True, "apps": self.app_states()}
        if action in {"launch_app", "close_app"}:
            name = command.get("app")
            if name not in self.allowed_apps:
                return {"ok": False, "error": "App is not in the local allowlist"}
            if action == "launch_app":
                current = self.processes.get(name)
                if current is not None and current.poll() is None:
                    return {"ok": True, "message": "Already running"}
                self.processes[name] = subprocess.Popen(
                    self.allowed_apps[name],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                    start_new_session=(os.name != "nt"),
                )
                return {"ok": True, "message": "Started"}

            process = self.processes.pop(name, None)
            if process is None or process.poll() is not None:
                return {"ok": False, "error": "Not running (only apps started by this agent can be closed)"}
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            return {"ok": True, "message": "Closed"}

        return {"ok": False, "error": "Unsupported action"}

    def request_json(self, path, payload):
        response = self.session.post(
            f"{self.base_url}{path}", json=payload, timeout=20
        )
        response.raise_for_status()
        return response.json()

    def run(self):
        LOGGER.info("Agent connected to %s with %d allowed app(s)", self.base_url, len(self.allowed_apps))
        registration = self.request_json("/api/agent/register", {})
        pairing_code = registration.get("code")
        if not isinstance(pairing_code, str) or len(pairing_code) != 3:
            raise RuntimeError("The server returned an invalid pairing code")
        print(
            f"Enter this 3-character code in the web app within 5 minutes: {pairing_code}",
            flush=True,
        )
        while True:
            try:
                response = self.request_json(
                    "/api/agent/next", {"apps": list(self.allowed_apps)}
                )
                command = response.get("command")
                if command:
                    LOGGER.info("Running action %s", command.get("action"))
                    try:
                        result = self.execute(command)
                    except Exception as error:
                        LOGGER.exception("Command failed")
                        result = {"ok": False, "error": str(error)[:300]}
                    self.request_json(f"/api/agent/results/{command['id']}", result)
                else:
                    time.sleep(2)
            except (requests.RequestException, ValueError, KeyError) as error:
                LOGGER.warning("Server request failed: %s", error)
                time.sleep(5)


def main():
    if REMOTE_CONTROL_URL == "https://your-project.vercel.app":
        raise SystemExit("Set REMOTE_CONTROL_URL near the top of pc_client.py")
    if not AGENT_TOKEN or AGENT_TOKEN == "paste-the-same-agent-token-configured-in-vercel":
        raise SystemExit("Set AGENT_TOKEN near the top of pc_client.py")
    PcAgent(REMOTE_CONTROL_URL, AGENT_TOKEN, load_allowed_apps()).run()


if __name__ == "__main__":
    main()