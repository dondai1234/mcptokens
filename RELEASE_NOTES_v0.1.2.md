# mcptokens 0.1.2

PyPI metadata cleanup. The Python package on the new repo
(`dondai1234/mcptokens`) had stale URLs that pointed at the
abandoned `dondai1234/contextlens` repo. The wheel's
`project_urls` table now points at `mcptokens`, the README
cites the new repo, and the install flow is unchanged:

```bash
pip install mcptokens
```

Everything else (engine, server, CLI, tests, README, self-cost
of 172 tokens) stays at 0.1.1.
