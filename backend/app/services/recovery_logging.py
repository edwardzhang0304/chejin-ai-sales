"""Safe recovery evidence; no exception text, payloads or traceback locals."""
import logging
from pathlib import PurePath


def recovery_log(logger: logging.Logger, level: int, message: str, *args: object) -> None:
    # A broken logging handler must not prevent another recovery stage. Do not
    # catch BaseException: shutdown and process-control exceptions still escape.
    try:
        logger.log(level, message, *args)
    except Exception:
        pass


def recovery_failure(logger: logging.Logger, stage: str, exc: Exception, **business_ids: object) -> None:
    # Never log str(exc), SQL parameters, request payloads or traceback locals.
    # Keep the source location and actual IDs when the caller has already found
    # an item. A stage-wide query/init failure has no known item ID.
    tb = exc.__traceback__
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    location = (f"{PurePath(tb.tb_frame.f_code.co_filename).name}:"
                f"{tb.tb_frame.f_code.co_name}:{tb.tb_lineno}" if tb else "unavailable")
    recovery_log(logger, logging.ERROR,
                  "C3 recovery failed stage=%s error_type=%s location=%s business_ids=%s",
                  stage, type(exc).__name__, location, business_ids or "unavailable_before_item_selection")
