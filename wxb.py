#!/usr/bin/env python3
"""WXB entrypoint — the studio shell.

``wxb_engine.py`` contains the input engines; ``wxb_studio.py`` owns the GTK4
interface. Run ``python3 wxb.py`` to open it.
"""

from wxb_studio import main

if __name__ == "__main__":
    raise SystemExit(main())
