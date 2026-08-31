# insightops_vuddy

A Claude Code plugin marketplace hosting one plugin: **insightops-buddy** — autonomous end-to-end
incident response for failed Databricks production job runs (diagnose, ticket, fix, PR, review,
verify, alert, across 11 phases). See `plugins/insightops-buddy/README.md` for what it does and
how it's put together.

## Install

```
/plugin marketplace add mohamedarshath-coder/insightops_vuddy
/plugin install insightops-buddy@insightops-vuddy2
```

Then complete the one-time MCP server setup described in
`plugins/insightops-buddy/mcp-server/README.md` (create its Python venv, install its
dependencies) — the plugin declares how to launch that server, but doesn't install its
dependencies for you.

## Structure

```
insightops_vuddy/
├── .claude-plugin/
│   └── marketplace.json          # this marketplace's catalog (lists the plugins below)
└── plugins/
    └── insightops-buddy/          # the plugin itself
```

## Updating

Push changes to this repo, then from a client with the marketplace already added:

```
/plugin marketplace update
```
