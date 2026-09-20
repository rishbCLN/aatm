"""Make ``python -m aatm.cli`` work."""

from .commands import main

if __name__ == "__main__":
    import sys

    sys.exit(main())
