"""Terminal adapter using the same session service as the browser."""

import json
import time

from ..application.sessions import SessionService


def run(config, client_factory):
    application = SessionService(config, client_factory)
    application.start()
    session_id = application.create()["id"]
    cursor = 0
    print("Agent Harness. Enter a question; q exits.\n")
    try:
        while True:
            query = input("agent >> ").strip()
            if not query or query.lower() in ("q", "quit", "exit"):
                break
            application.submit(session_id, query)
            session = application.get(session_id)
            while session.status == "running":
                for event in application.repository.events(session_id, cursor):
                    cursor = event["id"]
                    kind, payload = event["kind"], event["payload"]
                    if kind == "assistant" and payload.get("scope", "agent") == "agent":
                        print(payload["text"])
                    elif kind == "tool_started":
                        print(f"> {payload['name']}")
                    elif kind == "error":
                        print(payload["message"])
                for approval in session.approvals.snapshot():
                    print(
                        f"\n{approval['name']}: {json.dumps(approval['input'], ensure_ascii=False)}"
                    )
                    allow = input("Allow? [y/N] ").strip().lower() in ("y", "yes")
                    session.approvals.answer(approval["id"], allow)
                time.sleep(0.05)
            for event in application.repository.events(session_id, cursor):
                cursor = event["id"]
                if (
                    event["kind"] == "assistant"
                    and event["payload"].get("scope", "agent") == "agent"
                ):
                    print(event["payload"]["text"])
                elif event["kind"] == "error":
                    print(event["payload"]["message"])
    except (EOFError, KeyboardInterrupt):
        application.cancel(session_id)
    finally:
        application.close()
