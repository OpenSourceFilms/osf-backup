# Dependency licensing and source provenance

**Review snapshot:** 26 October 2026. This document identifies the **upstream source
licences** corresponding to all 14 names in `requirements.lock.txt`, based on
licence files in the publishers' official repositories on the review date.
It is a source-level reference, **not** a legal clearance or a complete
software bill of materials for an installed Python environment.

**Important:** this historical public repository has **no root open-source
licence**. Its published visibility does not automatically grant permission
to copy, modify or redistribute OSF-authored source code. See the
[unresolved owner/licence decision](https://github.com/OpenSourceFilms/osf-backup/issues/1).
The permissive licences below cover **their respective upstream projects**;
they do **not** grant rights to the independent OSF backup implementation.

## Published Python dependency licences

The package names and pinned versions below come from the current public
`requirements.lock.txt`. The licence classifications describe the upstream
source, not separately downloaded wheels, build-time options, bundled C
libraries or all dependencies they might import.

| Pinned package | Published upstream source licence | Official source |
|---|---|---|
| `boto3==1.43.72` | Apache-2.0 | [boto/boto3 LICENSE](https://github.com/boto/boto3/blob/develop/LICENSE) |
| `botocore==1.43.72` | Apache-2.0 | [boto/botocore LICENSE.txt](https://github.com/boto/botocore/blob/develop/LICENSE.txt) |
| `iniconfig==2.3.0` | MIT | [pytest-dev/iniconfig LICENSE](https://github.com/pytest-dev/iniconfig/blob/main/LICENSE) |
| `jmespath==1.1.0` | MIT | [jmespath/jmespath.py LICENSE](https://github.com/jmespath/jmespath.py/blob/develop/LICENSE) |
| `packaging==26.3` | Apache-2.0 **or** BSD-2-Clause | [pypa/packaging dual grant](https://github.com/pypa/packaging/blob/main/LICENSE) |
| `pluggy==1.6.0` | MIT | [pytest-dev/pluggy LICENSE](https://github.com/pytest-dev/pluggy/blob/main/LICENSE) |
| `pygments==2.20.0` | BSD-2-Clause | [pygments LICENSE](https://github.com/pygments/pygments/blob/master/LICENSE) |
| `pytest==9.1.1` | MIT | [pytest-dev/pytest LICENSE](https://github.com/pytest-dev/pytest/blob/main/LICENSE) |
| `python-dateutil==2.9.0.post0` | BSD-3-Clause, with Apache-2.0 grant for applicable portions | [dateutil LICENSE](https://github.com/dateutil/dateutil/blob/master/LICENSE) |
| `pyyaml==6.0.3` | MIT-style | [yaml/pyyaml LICENSE](https://github.com/yaml/pyyaml/blob/main/LICENSE) |
| `s3transfer==0.19.2` | Apache-2.0 | [boto/s3transfer LICENSE.txt](https://github.com/boto/s3transfer/blob/develop/LICENSE.txt) |
| `six==1.17.0` | MIT-style | [benjaminp/six LICENSE](https://github.com/benjaminp/six/blob/main/LICENSE) |
| `urllib3==2.7.0` | MIT | [urllib3 LICENSE.txt](https://github.com/urllib3/urllib3/blob/main/LICENSE.txt) |
| `zstandard==0.25.0` | BSD-3-Clause | [indygreg/python-zstandard LICENSE](https://github.com/indygreg/python-zstandard/blob/main/LICENSE) |

**Evidence limits:** these links refer to upstream branches current on the
review date, **not** an independently audited tag/release artifact for
every pinned wheel. Licence texts, NOTICE obligations, C library source,
optional compiled extensions and vendored dependencies can differ between
releases. Before repackaging or distributing a combined binary, verify
the precise wheel/sdist/container contents and preserve required notices.
Tests download the packages for their disposable CI environment; the public
Git tree does not vendor them.

## Provenance of the historical backup implementation

The public repository contains original-looking application modules in
`app/`, a CLI in `bin/backupctl`, configuration and a large synthetic
test suite. A source-tree inspection found **no obvious checked-in copy of
boto3, botocore, zstandard, PyYAML or other whole upstream package trees**.
The files import those dependencies at runtime; this does **not** establish
the legal authorship of every original or adapted source line.

This limited review has **not** established:

- Who owns or is authorised to license each code contribution, including
  any contractor, collaborator or AI-assisted contribution.
- Whether any isolated functions were adapted from external code not marked
  by a licence header. Absence of a copyright header is not proof of ownership.
- Chain of title for historical files or subsequently removed Git objects.
- Copyright/terms for configuration, host-specific deployment notes or any
  material copied into the historical handover documentation.
- Rights to reuse external S3/RunPod services, their APIs, credentials or data.
- Suitability, security or operating status of the current B2/restic backup.

**Disposition:** keep the original [licence-decision issue](https://github.com/OpenSourceFilms/osf-backup/issues/1)
open until an authorised owner has reviewed contributors and external
materials and approved the intended licence. Do not silently add MIT or
another root grant merely because the dependencies are permissively
licensed.

## Maintenance rule

When `requirements.lock.txt` changes, update the corresponding rows and
source evidence here in the same PR. The repository's offline tests check
that the full list of pinned dependency names is represented; they **cannot
verify legal correctness of the stated licences** and must not be
described as legal acceptance.

This file documents upstream copyright/licence facts; it does not itself
change copyright ownership, grant reuse rights or modify any service.
