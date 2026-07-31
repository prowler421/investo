"""No company CIK is derived from an accession.

The leading ten digits identify the *submitter*, which for most companies is a filer agent. Apple's
own history contains both patterns, so the wrong rule produces correct answers on some filings and a
nonexistent CIK on others. `Accession` therefore exposes no `cik` at all, and the absence is the
enforcement — this file attempts the access and basedpyright must reject it.
"""

from investo.domain.provenance import Accession

accession = Accession.parse("0001140361-26-025622")
_ = accession.cik  # ERROR: Accession has no `cik` attribute, deliberately
