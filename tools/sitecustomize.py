"""Install probes before spawn workers import the executor's initializer."""

import os

if os.environ.get("TYPE_TRACE_ROOT"):
    import trace_types
