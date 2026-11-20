"""`python -m osv360` (and the frozen AppImage binary): GUI without arguments, CLI otherwise."""
import sys

from osv360.cli import main

if __name__ == "__main__":
    sys.exit(main())
