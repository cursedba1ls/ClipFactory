"""PyInstaller entry point of ClipFactory.exe."""
import sys

from backend.desktop import main

if __name__ == "__main__":
    sys.exit(main())
