# Contributing

Use GitHub Issues for reproducible bugs and focused proposals. Include the expected behavior, actual behavior, relevant versions, and synthetic evidence. Never attach credentials, customer conversations, private endpoints, database dumps, or real report exports.

Keep each pull request focused on one change. Explain its behavior and validation, add meaningful regression coverage, and run the relevant test, lint, and frontend build commands in the [development guide](docs/DEVELOPMENT.md). Use a dedicated disposable database for integration tests.

## Configuration changes

When introducing or changing a setting, update these together:

1. The configuration definition and validation in code.
2. `.env.example`, with service addresses, resource identifiers, and credentials left empty.
3. `docs/OPERATIONS.md`, including the purpose, format, and required conditions.
4. Both README versions if startup steps or minimum requirements change.
5. Regression coverage for configuration boundaries where behavior changes.

The README is the entry point, `.env.example` is the copyable variable template, and the configuration document is the detailed reference. Do not duplicate private deployment values across them. Non-secret behavior defaults such as local development mode may be included when clearly documented.

## Documentation and data

Keep `README.md` and `README.zh-CN.md` synchronized. Check relative links and commands after edits. Use synthetic hosts and fixtures in tests; never copy production incident records or customer examples into the public repository.

Publish Prompt behavior changes as new versions, and preserve the distinction between collected evidence, model judgments, and human reviews. Passing tests does not establish that a live integration or business scenario succeeds.
