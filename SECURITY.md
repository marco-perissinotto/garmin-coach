# Security policy

garmin-coach handles a Garmin API key and personal health data, so security reports are taken seriously.

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's private reporting instead: on the repository page go to
**Security → Report a vulnerability**. Describe the problem and how to reproduce it, and leave out real keys, tokens
and health data.

You will get an answer as soon as possible; this is a personal project maintained in spare time, so allow a few days.

## In scope

- Any way the API key or a bearer token could end up in logs, error messages, files in the repository or process
  arguments.
- Any request other than a `GET` (plus the token exchange) reaching Garmin.
- The MCP server listening on anything other than stdio or `127.0.0.1`, or the HTTP mode accepting requests without
  the bearer token.
- Personal data written outside the local app-data directory.

## If you leaked your own key

Delete it in Garmin Connect (Settings → Personal API) and create a new one: deleting a key invalidates every token
derived from it immediately.
