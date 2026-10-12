"""``python -m acb`` runs the ``acb`` command (used by the launcher for child processes)."""

from acb.cli import main

raise SystemExit(main())
