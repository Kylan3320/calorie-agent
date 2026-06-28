"""Product validation metrics for V3.

The package is opt-in. Importing it does not enable the online V3 write path.
"""

from .events import (
    EventType,
    build_usage_event,
    build_usage_events_from_runtime_result,
    build_usage_events_from_trace,
    normalize_usage_event,
    record_usage_event,
    safe_record_usage_event,
)
from .feedback import (
    build_feedback_sample,
    detect_feedback,
    record_feedback_sample,
    safe_record_feedback_sample,
)
from .retention import (
    calculate_activation,
    calculate_d1_retention,
    calculate_d7_retention,
    calculate_w4_retention,
)

__all__ = [
    "EventType",
    "build_feedback_sample",
    "build_usage_event",
    "build_usage_events_from_runtime_result",
    "build_usage_events_from_trace",
    "calculate_activation",
    "calculate_d1_retention",
    "calculate_d7_retention",
    "calculate_w4_retention",
    "detect_feedback",
    "normalize_usage_event",
    "record_feedback_sample",
    "record_usage_event",
    "safe_record_feedback_sample",
    "safe_record_usage_event",
]
