# Security policy

## Reporting a vulnerability

Please report vulnerabilities **privately**, not in public issues or pull requests:

- use the repository's private vulnerability reporting ("Report a vulnerability"
  under the Security tab), or
- email the maintainers at the address listed in the repository profile.

Include the affected version, a description, and steps to reproduce. We aim
to acknowledge within 3 working days and to agree a disclosure timeline with
you. If skyctl becomes an Apache project, reports will go to
security@apache.org and follow the [ASF security process](https://www.apache.org/security/).

## Supported versions

Security fixes are made on the latest minor release.

## Scope: what we especially want to hear about

skyctl holds Bluesky credentials, cloud credentials and, in agent mode,
acts on behalf of accounts. Reports in these areas are particularly valuable:

| Area | Examples |
|---|---|
| Approval bypass | executing a plan that was not approved, or a plan different from the one approved (hash binding), approving with the agent's own credential |
| Policy bypass | a direct write from a `plan`-profile session; acting outside the account scope; exceeding write budgets or deployment ceilings |
| Prompt injection with effect | content from posts, replies, profiles or web pages causing an action that policy or approval should have prevented |
| SSRF | `fetch_page`, link cards or persona sources reaching private, loopback, link-local or metadata addresses (including via redirects or DNS) |
| Credential exposure | app passwords, session strings, tokens or cloud credentials in logs, run transcripts, plan records, Terraform output or error messages |
| Authentication | bypassing the HTTP auth chain (`bearer`, OIDC, ALB, Easy Auth), token confusion between agent and approver |
| Idempotency | duplicate posts under retries, redelivery or crashes |

## Hardening guidance for operators

See [docs/OPERATIONS.md](docs/OPERATIONS.md): least-privilege IAM, separate
approver credentials, the `plan` profile for agents, out-of-band approval
for public accounts, and rotating app passwords.
