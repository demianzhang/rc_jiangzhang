"""Local demonstration supplier; its counters are intentionally not persistent."""

from collections import Counter

from fastapi import FastAPI, Request, Response


app = FastAPI(title="Demo supplier")
attempts: Counter[str] = Counter()


@app.api_route("/{scenario}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def receive(scenario: str, request: Request):
    notification_id = request.headers.get("X-Notification-Id", "missing")
    attempts[notification_id] += 1
    if scenario == "always-fail":
        return Response(status_code=503, headers={"Retry-After": "1"})
    if scenario == "flaky" and attempts[notification_id] <= 2:
        return Response(status_code=503, headers={"Retry-After": "1"})
    if scenario == "reject":
        return Response(status_code=400)
    if scenario in {"flaky", "success"}:
        return Response(status_code=204)
    return Response(status_code=404)
