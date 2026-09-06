import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx


class EndToEndTests(unittest.TestCase):
    def test_real_http_retry_and_process_restart(self):
        counts = Counter()
        received = []
        delivery_started = threading.Event()
        release_delivery = threading.Event()

        class Supplier(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                notification_id = self.headers["X-Notification-Id"]
                counts[notification_id] += 1
                received.append((notification_id, body, self.headers["Content-Type"]))
                if self.path == "/hold" and counts[notification_id] == 1:
                    delivery_started.set()
                    release_delivery.wait(timeout=10)
                    return
                if self.path == "/reject":
                    code = 400
                else:
                    code = 503 if counts[notification_id] == 1 else 204
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.send_header("Retry-After", "0")
                self.end_headers()

            def log_message(self, format, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Supplier)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        processes = []
        client = httpx.Client(trust_env=False, timeout=0.5)
        with tempfile.TemporaryDirectory() as directory:
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                api_port = reservation.getsockname()[1]
            base = f"http://127.0.0.1:{api_port}"
            supplier = f"http://127.0.0.1:{server.server_port}"
            env = {
                **os.environ,
                "NOTIFY_API_KEY": secrets.token_hex(32),
                "NOTIFY_ALLOWED_ORIGINS": supplier,
                "NOTIFY_DB_PATH": str(Path(directory) / "queue.sqlite3"),
                "NOTIFY_MAX_ATTEMPTS": "3",
                "NOTIFY_POLL_SECONDS": "0.02",
                "NOTIFY_RETRY_BASE_SECONDS": "0.05",
                "NOTIFY_RETRY_CAP_SECONDS": "0.1",
                "NOTIFY_ATTEMPT_TIMEOUT_SECONDS": "1",
                "NOTIFY_LEASE_SECONDS": "6",
            }
            headers = {"X-API-Key": env["NOTIFY_API_KEY"], "Idempotency-Key": "event-1"}
            payload = {
                "url": supplier + "/flaky", "method": "POST",
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"event": "user.registered", "user_id": 42}),
            }

            def start(*args):
                process = subprocess.Popen(
                    [sys.executable, *args], cwd=Path(__file__).resolve().parents[1],
                    env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                )
                processes.append(process)
                return process

            def stop(process):
                if process.poll() is None:
                    process.terminate()
                try:
                    output, _ = process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    output, _ = process.communicate(timeout=5)
                return output

            def start_api():
                process = start(
                    "-m", "uvicorn", "notify_service.api:create_app", "--factory",
                    "--host", "127.0.0.1", "--port", str(api_port), "--no-access-log",
                )
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        self.fail(f"API exited: {stop(process)}")
                    try:
                        if client.get(base + "/healthz").status_code == 200:
                            return process
                    except httpx.TransportError:
                        pass
                    time.sleep(0.05)
                self.fail(f"API did not become ready: {stop(process)}")

            def await_terminal(notification_id, worker):
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if worker.poll() is not None:
                        self.fail(f"Worker exited: {stop(worker)}")
                    result = client.get(
                        base + f"/v1/notifications/{notification_id}", headers=headers,
                    )
                    self.assertEqual(result.status_code, 200, result.text)
                    if result.json()["status"] in {"succeeded", "dead"}:
                        return result.json()
                    time.sleep(0.05)
                self.fail(f"Delivery timed out: {stop(worker)}")

            try:
                api = start_api()
                first = client.post(base + "/v1/notifications", headers=headers, json=payload)
                self.assertEqual(first.status_code, 202, first.text)
                notification_id = first.json()["id"]
                self.assertEqual(received, [])
                stop(api)
                api = start_api()
                duplicate = client.post(base + "/v1/notifications", headers=headers, json=payload)
                self.assertTrue(duplicate.json()["deduplicated"])
                self.assertEqual(duplicate.json()["id"], notification_id)

                worker = start("-m", "notify_service.worker")
                result = await_terminal(notification_id, worker)
                self.assertEqual(result["status"], "succeeded")
                self.assertEqual(result["attempts"], 2)
                self.assertEqual([item["http_status"] for item in result["history"]], [503, 204])
                self.assertEqual(counts[notification_id], 2)
                self.assertTrue(all(item[1] == payload["body"].encode() for item in received))
                self.assertTrue(all(item[2] == "application/json" for item in received))

                stop(worker)
                headers["Idempotency-Key"] = "event-2"
                second = client.post(base + "/v1/notifications", headers=headers, json=payload)
                self.assertEqual(second.status_code, 202, second.text)
                second_id = second.json()["id"]
                self.assertEqual(counts[second_id], 0)
                worker = start("-m", "notify_service.worker")
                self.assertEqual(await_terminal(second_id, worker)["status"], "succeeded")

                headers["Idempotency-Key"] = "event-3"
                rejected = client.post(
                    base + "/v1/notifications", headers=headers,
                    json={**payload, "url": supplier + "/reject"},
                )
                self.assertEqual(rejected.status_code, 202, rejected.text)
                rejected_id = rejected.json()["id"]
                dead = await_terminal(rejected_id, worker)
                self.assertEqual(dead["status"], "dead")
                self.assertEqual(dead["attempts"], 1)
                redrive = client.post(
                    base + f"/v1/notifications/{rejected_id}/redrive", headers=headers,
                )
                self.assertEqual(redrive.status_code, 202, redrive.text)
                dead_again = await_terminal(rejected_id, worker)
                self.assertEqual(dead_again["total_attempts"], 2)
                self.assertEqual(counts[rejected_id], 2)

                headers["Idempotency-Key"] = "event-4"
                interrupted = client.post(
                    base + "/v1/notifications", headers=headers,
                    json={**payload, "url": supplier + "/hold"},
                )
                self.assertEqual(interrupted.status_code, 202, interrupted.text)
                interrupted_id = interrupted.json()["id"]
                self.assertTrue(delivery_started.wait(timeout=5), "Supplier never received the request")
                worker.kill()
                stop(worker)
                release_delivery.set()
                worker = start("-m", "notify_service.worker")
                recovered = await_terminal(interrupted_id, worker)
                self.assertEqual(recovered["status"], "succeeded")
                self.assertEqual(counts[interrupted_id], 2)
                self.assertEqual(
                    [item["outcome"] for item in recovered["history"]], ["unknown", "succeeded"],
                )
                self.assertEqual(recovered["history"][0]["error"], "lease_expired")
            finally:
                release_delivery.set()
                for process in reversed(processes):
                    stop(process)
                client.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
