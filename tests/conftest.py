"""Qt needs a platform plugin even for offscreen image work in CI/nix builds."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
