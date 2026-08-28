# fitz metadata compatibility shim

`tooluniverse==1.4.0` requires the retired PyPI distribution `fitz`, although
the runtime import named `fitz` is supplied by `PyMuPDF`. This empty local
distribution satisfies that outdated package metadata and depends on PyMuPDF;
it deliberately installs no Python module, so it cannot shadow PyMuPDF.
