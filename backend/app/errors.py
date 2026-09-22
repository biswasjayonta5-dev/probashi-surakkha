"""Small shared helpers: structured logging with PII redaction, typed errors."""

from __future__ import annotations

import json
import sys
import time
from typing import Any

_REDACT_KEYS = ("text", "document", "content", "raw", "body")


def log_event(event: str, **fields: Any) -> None:
    """One JSON line per event on stderr. Never log raw document text or PII."""
    from .safety.guard import redact_pii

    payload: dict[str, Any] = {"ts": round(time.time(), 3), "event": event}
    for key, value in fields.items():
        if key in _REDACT_KEYS and isinstance(value, str):
            payload[key] = redact_pii(value).text[:500]
        elif isinstance(value, str):
            payload[key] = redact_pii(value).text[:500]
        else:
            payload[key] = value
    sys.stderr.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stderr.flush()


class FraudShieldError(Exception):
    """Base class for errors that map to a friendly Bangla message."""

    message_bn = "কিছু ভুল হয়েছে। আবার চেষ্টা করুন।"

    def __init__(self, detail: str = "") -> None:
        super().__init__(detail or self.message_bn)
        self.detail = detail


class LLMUnavailable(FraudShieldError):
    message_bn = (
        "এখন এআই সেবা পাওয়া যাচ্ছে না। আপনার ফাইল বিশ্লেষণ করা যায়নি। "
        "লাইসেন্স যাচাই ও ফি যাচাই এখনো কাজ করছে। একটু পরে আবার চেষ্টা করুন।"
    )


class UnsupportedFile(FraudShieldError):
    message_bn = "এই ধরনের ফাইল সাপোর্ট করা হয় না। ছবি (JPG/PNG) বা PDF দিন।"


class FileTooLarge(FraudShieldError):
    message_bn = "ফাইলটি অনেক বড়। ছোট ফাইল বা কম রেজোলিউশনের ছবি দিন।"


class EmptyDocument(FraudShieldError):
    message_bn = (
        "ফাইল থেকে কোনো লেখা পড়া যায়নি। ছবিটি ঝাপসা হতে পারে। "
        "পরিষ্কার আলোতে পুরো চুক্তির ছবি আবার তুলুন।"
    )


class RateLimited(FraudShieldError):
    message_bn = "আপনি অল্প সময়ে অনেকবার চেষ্টা করেছেন। কিছুক্ষণ পরে আবার চেষ্টা করুন।"
