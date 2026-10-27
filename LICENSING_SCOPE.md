# Licence scope and third-party exclusions

The root [MIT licence](LICENSE) applies to original code, tests, scripts,
configuration examples and documentation **only insofar as Open Source Films
(OSF) or its contributors own or control the rights necessary to license
those contributions**. The licence does not override third-party ownership
or any separately stated terms.

The Git history includes original commits credited to Daniel and subsequent
OSF-linked maintainers. That is useful provenance evidence, but not a complete
independent copyright-assignment audit. Please report an apparent third-party
contribution that OSF cannot license in
[issue #1](https://github.com/OpenSourceFilms/osf-backup/issues/1).

The root MIT licence does **not** purport to relicense:
- External Python packages, including all packages listed in
  [DEPENDENCY_LICENSES.md](DEPENDENCY_LICENSES.md), their wheels, vendored
  libraries, or external software used by the old backup runtime.
- Any separately owned code, copied assets, credentials, backups or data
  for which OSF lacks the right to grant permission.
- Data and systems touched by a backup or restore. A licence for the backup
  tool does not transfer rights in the data it processes.

**Historical implementation:** This is not the current B2/restic production
backup system. The MIT grant does not certify deployment suitability,
successful real-world recovery, the security of its historical settings, or
the safety of running archived commands against a live filesystem.
