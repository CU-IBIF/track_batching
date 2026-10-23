"""PyInstaller entry point: no arguments opens the GUI, otherwise acts as the CLI."""
import sys

from trackbatch.cli import main

if __name__ == "__main__":
    sys.exit(main())
