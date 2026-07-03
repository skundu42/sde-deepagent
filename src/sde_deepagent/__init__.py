"""sde-deepagent — self-hostable software developer agent system built on deepagents."""

from importlib.metadata import PackageNotFoundError, version

try:
    # pyproject.toml is the single source of truth (a hardcoded constant here
    # already drifted once: 0.3.0 shipped reporting 0.2.1)
    __version__ = version("sde-deepagent")
except PackageNotFoundError:  # running from a checkout without an install
    __version__ = "0.0.0+dev"
