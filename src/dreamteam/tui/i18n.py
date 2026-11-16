"""The terminal client's texts under their old name. They live in dreamteam/uitext.py (outside the tui package, so the
HTTP API's event texts can use them, docs/SPEC.md §21.6), and this module is that one: `from dreamteam.tui import
i18n`, `from .i18n import t` and a change of `i18n.TEXTS` reach the same module and tables."""

import sys

from .. import uitext

sys.modules[__name__] = uitext
