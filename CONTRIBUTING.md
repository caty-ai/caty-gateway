# Contributing

Open a focused change with tests and a clear compatibility note. New backends should implement the existing backend interface, define preflight behavior, document only public environment variables, and include unit plus smoke coverage. Run the test suite, the scrub audit, and gitleaks before submitting. Do not add credentials, private deployment paths, or personal test fixtures.
