import logging

import pytest

from app.masking import MaskingFilter, install_log_masking, mask_text, mask_value


@pytest.mark.parametrize(
    "text, expected",
    [
        ("write to jane.doe@example.com today", "write to [EMAIL] today"),
        ("a+tag@sub.example.co.uk", "[EMAIL]"),
        ("two: a@b.io and c_d@e-f.org", "two: [EMAIL] and [EMAIL]"),
    ],
)
def test_emails_are_masked(text, expected):
    assert mask_text(text) == expected


@pytest.mark.parametrize(
    "phone",
    [
        "(555) 014-2299",
        "(555)014-2299",
        "+1 555 014 2299",
        "+1-555-014-2299",
        "+44 20 7946 0958",
        "555-014-2299",
        "555.014.2299",
        "555 014 2299",
        "555-0142",
        "5550142299",
    ],
)
def test_phone_numbers_in_common_formats_are_masked(phone):
    assert mask_text(f"call {phone} now") == "call [PHONE] now"
    assert mask_text(f"call {phone}.") == "call [PHONE]."


@pytest.mark.parametrize(
    "text",
    [
        "A root canal costs $450 to $1,290.",
        "Prices run from $450 - $1,290 per tooth.",
        "See page 3 of the guide.",
        "Pages 3-4 and chunk 12.",
        "In 2026 we moved.",
        "Our ZIP code is 43001.",
        "Riverton, OH 43001-1234",
        "214 Maple Avenue, Suite 3, Riverton, OH 43001",
        "Closed on 2026-11-26 (Thanksgiving).",
        "Updated 11-17-2026.",
        "Open 8:00 am to 5:00 pm, 42 spaces, 24 hours, 2 to 4 hours.",
        "Similarity 0.4075 and score 87%.",
        "Only 1 to 4 sentences.",
    ],
)
def test_prices_dates_years_zip_codes_and_page_numbers_are_left_alone(text):
    assert mask_text(text) == text


def test_masking_is_idempotent():
    once = mask_text("mail a@b.com or call (555) 014-2299")
    assert once == "mail [EMAIL] or call [PHONE]"
    assert mask_text(once) == once


def test_mask_value_changes_strings_only():
    value = {
        "answer": "call (555) 014-2299",
        "ids": [101, 102],
        "similarity": 0.4075,
        "flag": True,
        "none": None,
        "nested": {"mail": "a@b.com", "n": 5550142299, "list": ("x@y.org", 3, 1.5, False)},
    }
    assert mask_value(value) == {
        "answer": "call [PHONE]",
        "ids": [101, 102],
        "similarity": 0.4075,
        "flag": True,
        "none": None,
        "nested": {"mail": "[EMAIL]", "n": 5550142299, "list": ["[EMAIL]", 3, 1.5, False]},
    }


def test_mask_value_does_not_change_the_numbers_even_when_they_look_like_a_phone_number():
    assert mask_value(5550142299) == 5550142299 and mask_value(0.5551422) == 0.5551422


def test_the_logging_filter_masks_the_message_and_its_arguments():
    record = logging.LogRecord(
        "x", logging.INFO, __file__, 1, "user %s asked %s", ("a@b.com", "(555) 014-2299"), None
    )
    assert MaskingFilter().filter(record) is True
    assert record.getMessage() == "user [EMAIL] asked [PHONE]"


def test_installing_the_log_masking_twice_adds_one_filter():
    handler = logging.StreamHandler()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        install_log_masking()
        install_log_masking()
        assert sum(isinstance(f, MaskingFilter) for f in handler.filters) == 1
    finally:
        root.removeHandler(handler)
