# Security and public-artifact policy

- Secrets are read from environment variables and are never accepted in committed configuration.
- Generated workspaces, SQLite state, trajectories, and local `.env` files are ignored.
- `python scripts/public_repo_audit.py` checks the tracked tree for token-shaped strings, personal home
  paths, and runtime databases. CI runs the same check on every push and pull request.
- If you find a security issue, open a private GitHub security advisory instead of a public issue.
