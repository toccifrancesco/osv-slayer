"""PyInstaller entry point: GUI without arguments (or with .OSV paths), CLI otherwise."""
import multiprocessing
import sys

from osv360.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
