# Hostile fixtures -- handle with care

These files are INERT test fixtures for ingestion security tests (TASK-TEST-002).
They MUST NOT be opened with desktop tools (Excel, Explorer preview, etc.) -- some are
mislabelled or macro-enabled and are meant only to be fed to the ingestion pipeline's
validators under test. `macro_enabled.xlsm` contains a placeholder `vbaProject.bin`
(not executable VBA bytecode) purely to trigger the macro-enabled-format detector.

See `labels.json` in this directory for the expected outcome of each file.
