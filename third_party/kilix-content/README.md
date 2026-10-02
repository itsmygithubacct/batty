# Kilix content library

Runtime source from [kilix-content](https://github.com/itsmygithubacct/kilix-content),
pinned with original file hashes in `upstream.json`. The MIT license is included.
Batty's adapted installer adds an explicit Git reinstall operation. It builds
the pinned source in staging and atomically exchanges it with a managed
checkout whose tracked files are clean, so an interrupted build leaves the
selected installation intact. Managed build output inside that checkout is
replaced; NVR recordings and configuration live outside it.
