# Security policy

## Supported versions

Before the first stable release, security fixes are made on `main` and included
in the next tagged release. Pre-1.0 releases may receive breaking security
hardening when preserving an unsafe interface would be worse than changing it.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use GitHub's private
[security advisory form](https://github.com/pineforge-4pass/pineforge-hpo/security/advisories/new)
and include:

- the affected version, tag, or commit;
- platform and compiler or Python version;
- a minimal reproduction or proof of concept;
- expected impact and any known mitigations;
- whether the issue also affects `pineforge-engine` or
  `pineforge-codegen-oss`.

Please avoid testing against systems, data, or accounts you do not own. The
maintainers will acknowledge a complete report, validate scope, coordinate a
fix, and credit reporters who want attribution. Public disclosure should wait
until a fix or mitigation is available.
