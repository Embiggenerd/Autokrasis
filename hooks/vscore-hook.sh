#!/bin/sh
# The vscore plugin's PreToolUse hook: runs `vscore hook claude-code`, which reads the tool call on stdin
# and denies it when it destroys more than twice the work in the code the bug is about. Without vscore
# installed it says so and exits 1, a non-blocking error, so the call goes ahead unchecked.
if ! command -v vscore >/dev/null 2>&1; then
  echo "vscore is not installed, so this call was not checked. Install it with:" >&2
  echo "  python3 -m pip install \"vscore @ git+https://github.com/Embiggenerd/Autokrasis\"" >&2
  exit 1
fi
exec vscore hook claude-code
