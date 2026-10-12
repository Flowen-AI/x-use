# Security reporting

Report suspected vulnerabilities privately to
[ihuzaifashoukat@gmail.com](mailto:ihuzaifashoukat@gmail.com). Use a subject such
as `X-Use security report` and describe the affected version, impact, relevant
configuration and a minimal reproduction using fictional data.

Do not put account cookies, tokens, inbox PINs, private messages, browser
profiles, unredacted configuration or exploit details in a public GitHub issue.
Do not send account credentials by email. If a reproduction needs sensitive
material, describe what is needed first so a safer exchange can be agreed.

Useful reports include:

- A path that exposes credentials or private account state.
- An action that reaches the wrong account or target.
- A way to execute an unapproved draft, bypass configured policy or replay an
  uncertain external write.
- A dependency or installation issue with a concrete security impact.

Identify whether the issue affects the latest published release, the development
branch or an older version. The project has no published long-term support or
response-time guarantee; reporting an older-version issue is still welcome.
Please coordinate disclosure while the maintainer assesses and fixes the issue.

Ordinary installation failures and feature requests belong in
[GitHub Issues](https://github.com/ihuzaifashoukat/x-use/issues). Include only
sanitized diagnostics. See [Contributing](CONTRIBUTING.md) for isolated testing
and the [Code of Conduct](CODE_OF_CONDUCT.md) for community reporting.
