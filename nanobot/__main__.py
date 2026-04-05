"""
Entry point for running nanobot as a module: python -m nanobot
"""
import sys

# Fast path: nanobot ark → delegate to nanobot.ark (avoids loading full nanobot)
if len(sys.argv) > 1 and sys.argv[1] == "ark":
    from ark.cli import app
    sys.argv = [sys.argv[0]] + sys.argv[2:]
else:
    from nanobot.cli.commands import app

if __name__ == "__main__":
    app()
