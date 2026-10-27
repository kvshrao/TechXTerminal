# Security policy

## Supported versions

Only the latest release receives security fixes. Please update to it before reporting.

## Reporting a vulnerability

Please do not report security problems in a public issue.

Use GitHub's private reporting instead: on the [Security tab](https://github.com/kvshrao/TechXTerminal/security/advisories/new) choose **Report a vulnerability**. Include:

- the version (click the logo to open About) and how you installed it (installer or portable);
- what you did, what happened, and what you expected;
- if it involves a server, what that server sent, in as small an example as you can make.

You will get an answer within a few days. A fix is released as soon as it is ready, and you are credited in the changelog unless you would rather not be.

## What counts

Of most interest: a server or a file it sends being able to write outside the folder you chose, run a program, or draw content as HTML; host-key checks that can be bypassed; saved passwords reaching a file, backup or log; agent forwarding turning on without being asked.

Known and documented: a password written after `-pw` in a session's command line is stored as plain text (the session window warns about it).

## Checking a download

Each release lists SHA-256 checksums in `SHA256SUMS.txt`. Compare with `Get-FileHash -Algorithm SHA256 <file>` before running it. The programs are not yet code-signed, so Windows SmartScreen may warn about an unknown publisher.
