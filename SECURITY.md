# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability, please report it responsibly. **Do not open a public issue.**

Email: **security@notely.dev** (or open a private security advisory on GitHub)

Please include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact

We will acknowledge your report within 48 hours and provide a timeline for a fix.

## Scope

**In scope:**
- SQL injection in search or query paths
- Secret leakage (values from `.secrets.toml` appearing in logs, API calls, or markdown)
- Path traversal in file operations
- Unsafe deserialization of user-provided data

**Out of scope:**
- The Anthropic API key is stored in `.env` in plaintext — this is by design (local-only tool)
- Content sent to the Anthropic API during normal operation (this is the tool's core function)
- MCP server access — the server is local-only and not exposed to the network
- Issues in third-party dependencies (report those upstream)

## Secret Handling

Notely's `|||secret|||` marker system is a convenience feature for local secret management. It masks values before API calls and stores them in `.secrets.toml`. It is **not** a security boundary — it's designed to prevent accidental leakage, not to resist a determined attacker with local access.
