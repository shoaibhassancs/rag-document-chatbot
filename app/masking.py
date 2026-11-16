"""Mask emails and phone numbers so personal data does not reach logs or stored chat history.

`mask_text` works on one string, `mask_value` walks dicts and lists (jsonb columns) and changes
strings ONLY: numbers, booleans, chunk ids and similarity scores pass through unchanged.
`MaskingFilter` masks log records, so the same rule applies to application log files.

The phone rule is deliberately narrow so prices, dates, times, years, page numbers and ZIP codes
are left alone. It errs toward masking things that look like phone numbers: a 7-digit local number
or a range written as "450-1290" (no dollar sign) can be masked. Masking the clinic's own phone
number in an answer is a known side effect: it is masked in logs only, never in the reply.
"""

import logging
import re

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")

# Tried in this order at each position. Every digit-started form must not follow a letter, digit,
# "$", "." or "-" (so "2026-11-17" and "$1,290" are never entered in the middle).
PHONE = re.compile(
    r"""
    (?:
        # +1 555 014 2299
        (?<![\w$])\+\d{1,3}[\s.-]?(?:\(\d{1,4}\)[\s.-]?)?\d{1,4}(?:[\s.-]?\d{2,4}){1,4}
      |
        # (555) 014-2299
        (?<![\w$.-])\(\d{3}\)\s?\d{3}[\s.-]?\d{4}
      |
        # 555-014-2299
        (?<![\w$.-])\d{3}[\s.-]\d{3}[\s.-]\d{4}
      |
        # 555-0142
        (?<![\w$.-])\d{3}[\s.-]\d{4}
      |
        # 5550142299
        (?<![\w$.-])\d{10,15}
    )
    (?![\w])
    """,
    re.VERBOSE,
)


def mask_text(text: str) -> str:
    return PHONE.sub("[PHONE]", EMAIL.sub("[EMAIL]", text))


def mask_value(value):
    """Mask every string inside a dict, list or tuple. Everything else is returned unchanged."""
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, dict):
        return {key: mask_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask_value(item) for item in value]
    return value


class MaskingFilter(logging.Filter):
    """Masks the formatted message of a log record (arguments are merged in first)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # a broken log call must not hide the record
            return True
        record.msg = mask_text(message)
        record.args = ()
        return True


def install_log_masking() -> None:
    """Attach the filter to the handlers of the root and uvicorn loggers. Safe to call twice."""
    for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access"):
        for handler in logging.getLogger(name).handlers:
            if not any(isinstance(f, MaskingFilter) for f in handler.filters):
                handler.addFilter(MaskingFilter())
