# Security

homelab-map runs with read access to your network gear and publishes a map of your network, so
security problems matter here.

## Reporting a vulnerability

Please **don't open a public issue** for a security problem. Instead, use GitHub's private
reporting: the **Security** tab of this repository → **Report a vulnerability**.

Include what you found, how to reproduce it, and what an attacker could do with it. You'll get a
reply as soon as possible, and credit in the fix if you'd like it.

## Scope

In scope: anything in this repository that could leak credentials or network details, let someone
change something through the app, or run code (for example through device or container names,
config values, or data returned by UniFi, Proxmox, Docker, Caddy or BookStack).

The deliberate trade-offs listed under **Security** in the README (the viewer has no login,
`verify_tls` is off by default, the socket proxy can read container settings) are documented
choices, but ideas for safer defaults are welcome as normal issues.
