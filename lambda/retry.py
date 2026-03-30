"""
Exponential-backoff retry wrapper for AWS API calls.
"""

import random
import time
from functools import wraps

from botocore.exceptions import ClientError

from config import logger

RETRYABLE_ERROR_CODES = frozenset({
    "ThrottlingException",
    "TooManyRequestsException",
    "RequestLimitExceeded",
    "ServiceUnavailable",
    "InternalServiceException",
})


def retry_with_backoff(
    max_retries: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    retryable_codes: frozenset = RETRYABLE_ERROR_CODES,
):
    """
    Decorator that retries the wrapped function on transient AWS errors
    using exponential backoff with jitter.
    """

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except ClientError as exc:
                    error_code = exc.response["Error"]["Code"]
                    if error_code not in retryable_codes or attempt == max_retries:
                        raise
                    last_exc = exc
                    delay = min(base_delay * (2 ** attempt) + random.uniform(0, 1), max_delay)
                    logger.warning(
                        "Retryable error '%s' on %s (attempt %d/%d) — retrying in %.1fs",
                        error_code,
                        func.__name__,
                        attempt + 1,
                        max_retries,
                        delay,
                    )
                    time.sleep(delay)
            # Should not reach here, but just in case
            raise last_exc  # type: ignore[misc]

        return wrapper

    return decorator
