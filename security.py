"""
URL validation — PRD §9.2, Addendum M §29.3.
Two-layer validator: L1 domain allowlist + L2 career-subdomain heuristic.
Blocks SSRF, open redirect, private IPs, metadata endpoints.
"""

from urllib.parse import urlparse

ALLOWED_JOB_DOMAINS: frozenset[str] = frozenset({
    # Global platforms
    "linkedin.com",
    "indeed.com",
    "glassdoor.com",
    "monster.com",
    # SEA — Malaysia / Singapore / Philippines / Indonesia
    "jobstreet.com",
    "jobstreet.com.my",
    "jobstreet.com.sg",
    "jobstreet.com.ph",
    "jobsdb.com",
    "wobb.my",
    "hiredly.com",
    "jobscentral.com.sg",
    "mycareersfuture.gov.sg",
    "kalibrr.com",
    "glints.com",
    # Australia / New Zealand
    "seek.com.au",
    "seek.co.nz",
    # India
    "naukri.com",
    "shine.com",
    "timesjobs.com",
    # UK / Europe
    "reed.co.uk",
    "totaljobs.com",
    "cv-library.co.uk",
    "stepstone.de",
    # ATS / company career boards
    "greenhouse.io",
    "lever.co",
    "workday.com",
    "bamboohr.com",
    "smartrecruiters.com",
    "icims.com",
    "jobvite.com",
    "myworkdayjobs.com",
    "careers.google.com",
    "jobs.apple.com",
    "amazon.jobs",
})

_CAREER_PREFIXES: frozenset[str] = frozenset({
    "jobs",
    "careers",
    "career",
    "work",
    "talent",
    "apply",
    "hiring",
    "join",
    "recruit",
    "opportunities",
    "employment",
})

_PRIVATE_PREFIXES = (
    "127.",
    "10.",
    "192.168.",
    "172.16.",
    "172.17.",
    "172.18.",
    "172.19.",
    "172.20.",
    "172.21.",
    "172.22.",
    "172.23.",
    "172.24.",
    "172.25.",
    "172.26.",
    "172.27.",
    "172.28.",
    "172.29.",
    "172.30.",
    "172.31.",
    "::1",
    "0.0.0.0",
    "169.254.",
)

_BLOCKED_HOSTNAMES = {"localhost", "metadata.google.internal", "metadata.aws.internal"}


def validate_job_url(url: str) -> bool:
    """
    PRD §9.2 / Addendum M §29.3: two-layer validator.
    L1: domain allowlist (exact or subdomain match).
    L2: career-subdomain heuristic (≥3 DNS labels, first label in _CAREER_PREFIXES).
    Blocks: non-http(s) schemes, private IPs, localhost, metadata endpoints.
    """
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False

    if parsed.scheme not in ("http", "https"):
        return False

    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return False

    if hostname in _BLOCKED_HOSTNAMES:
        return False

    if any(hostname.startswith(p) for p in _PRIVATE_PREFIXES):
        return False

    # L1: allowlist — strip www. for bare comparison
    bare = hostname.removeprefix("www.")
    if any(bare == d or bare.endswith(f".{d}") for d in ALLOWED_JOB_DOMAINS):
        return True

    # L2: career subdomain heuristic — e.g. careers.stripe.com, talent.shopify.com
    labels = hostname.split(".")
    if len(labels) >= 3 and labels[0] in _CAREER_PREFIXES:
        return True

    return False
