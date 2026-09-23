#!/usr/bin/env python3
"""Send a hostile synthetic MIME message through a real hermetic receiver."""
from __future__ import annotations

import os
import smtplib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from email.message import EmailMessage

from email_capture.core import CaptureError, Profile, allocate, assert_messages, await_messages, backend, release_allocation


def main() -> int:
    profile = Profile.load()
    selected = backend(profile)
    request = {"schema_version": "1.0", "entity": "manolii", "repository": "ai-starter-pack", "environment": "ci", "run_id": f"live-{profile.backend}"}
    allocation = allocate(request, profile)
    message = EmailMessage()
    message["From"] = "synthetic-sender@capture.test"
    message["To"] = allocation["recipient"]
    message["Subject"] = "restricted synthetic subject"
    message["X-Capture-Test"] = "opaque-header-value"
    message.set_content("Synthetic code 123456 and link https://example.test/recover?opaque=1")
    message.add_alternative("<p>Synthetic <a href='https://example.test/recover?opaque=1'>link</a></p>", subtype="html")
    message.add_attachment(b"synthetic attachment", maintype="application", subtype="octet-stream", filename="fixture.bin")
    with smtplib.SMTP(os.environ["EMAIL_CAPTURE_SMTP_HOST"], int(os.environ["EMAIL_CAPTURE_SMTP_PORT"]), timeout=10) as smtp:
        smtp.send_message(message)
    messages, _cursor = await_messages(selected, allocation, timeout=15, count=1, not_before=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 5)))
    assert_messages(messages, {"exact_count": 1, "extract": [{"kind": "code", "exact_count": 1}, {"kind": "link", "exact_count": 1}, {"kind": "header", "name": "X-Capture-Test", "exact_count": 1}]})
    if len(messages[0]["attachments"]) != 1:
        raise AssertionError("attachment metadata count mismatch")
    release_allocation(selected, allocation)
    if selected.list(allocation):
        raise AssertionError("allocation-scoped cleanup incomplete")
    print('{"schema_version":"1.0","result":"passed","message_count":1,"cleanup_state":"complete"}')
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CaptureError as error:
        print('{"schema_version":"1.0","result":"failed","error_code":"' + error.code + '","detail":"' + error.safe_detail + '"}')
        raise SystemExit(2) from None
    except Exception as error:
        print('{"schema_version":"1.0","result":"failed","error_code":"CONFORMANCE_FAILED","detail":"' + type(error).__name__ + '"}')
        raise SystemExit(2) from None
