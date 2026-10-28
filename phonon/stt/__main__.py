"""`python -m stt` — the pod's single entry point."""

import sys

from stt.supervisor import main

if __name__ == "__main__":
    sys.exit(main())
