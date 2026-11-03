"""Portable production review routing; master policy parity is regression-tested."""

from fnmatch import fnmatch

MODEL = "gpt-6.1-sol"
ESCALATION = (
    "**/*.sql",
    "**/auth/**",
    "**/crypto/**",
    "**/payment/**",
    "**/stripe/**",
    "schema.sql",
    "drizzle.config.*",
    "config.yaml",
    "model-routing.json",
    "fly.toml",
    "openapi.yaml",
    "trpc.*.ts",
)


def effort(lane, paths):
    # Security always gets the calibrated heavy tier; standard code keeps its
    # cheaper medium tier unless any changed path (including deletions) escalates.
    heavy = lane == "security" or any(
        fnmatch(path, pattern)
        or (pattern.startswith("**/") and fnmatch(path, pattern[3:]))
        for path in paths
        for pattern in ESCALATION
    )
    return "xhigh" if heavy else "medium"
