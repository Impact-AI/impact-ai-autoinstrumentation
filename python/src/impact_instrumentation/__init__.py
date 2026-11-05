# SPDX-License-Identifier: Apache-2.0

"""impact.ai Python auto-instrumentation: the code that runs inside a customer's application process.

The modules `detect` and `setup_report` use the standard library only: they run before any application code and
must work (and stand down) without importing anything the application might own.
"""

from impact_instrumentation._version import __version__

__all__ = ["__version__"]
