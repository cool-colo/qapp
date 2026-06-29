# Repository Instructions

- Business logic and trading strategy behavior must use Nautilus first.
- Do not call QMT proxy APIs or QMT adapter internals directly for business logic or strategy behavior unless the user explicitly requests it.
- Treat direct proxy/adapter access as infrastructure plumbing only, not strategy logic.
