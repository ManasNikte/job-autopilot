import pytest

from app import salary
from app.sources.directory import board_from_url


@pytest.mark.parametrize("text,expected", [
    ("The base pay range is $120,000 - $160,000 USD per year.", (120000, 160000)),
    ("$45 - $60 per hour", (93600, 124800)),
    ("salary $120K/yr - $150K/yr", (120000, 150000)),
    ("SF: $150,000—$190,000 USD; NYC: $140,000—$180,000 USD", (140000, 190000)),
    ("up to $90,000 per year", (90000, 90000)),
    ("We raised $50M - $80M in funding", None),
    ("5+ years, $5 - $10", None),
])
def test_salary_parse(text, expected):
    assert salary.parse(text) == expected


def test_pay_floor_only_blocks_listed_low_pay():
    low = salary.annotate({"description": "Compensation: $70,000 - $85,000"})
    assert "pay below $100K" in salary.below_floor(low, 100000)
    assert salary.below_floor(salary.annotate({"description": "No pay listed"}), 100000) is None
    assert salary.below_floor(salary.annotate({"description": "$90K - $130K"}), 100000) is None


@pytest.mark.parametrize("url,expected", [
    ("https://job-boards.greenhouse.io/stripe/jobs/123", ("greenhouse", "stripe")),
    ("https://boards.greenhouse.io/embed/job_app?for=figma&token=1", ("greenhouse", "figma")),
    ("https://jobs.lever.co/plaid/abc-123/apply", ("lever", "plaid")),
    ("https://jobs.ashbyhq.com/ramp/uuid", ("ashby", "ramp")),
    ("https://bp.wd3.myworkdayjobs.com/en-US/bpCareers/job/Houston/Graduate_R1", ("workday", "bp.wd3.myworkdayjobs.com|bp|bpCareers")),
    ("https://jobs.smartrecruiters.com/Visa/7440001", ("smartrecruiters", "Visa")),
    ("https://example.com/careers/123", None),
])
def test_board_from_url(url, expected):
    assert board_from_url(url) == expected


@pytest.mark.parametrize("text", [
    "Uline does not participate in the H-1B lottery",
    "We are unable to offer visa sponsorship at this time.",
    "Vanguard is not offering visa sponsorship for this position.",
    "Applicants must be authorized to work in the United States on a permanent basis.",
])
def test_sponsorship_refusals_are_blockers(text):
    from app import prefilter
    from app.config import search_config
    job = {"title": "Software Engineer", "location": "Austin, TX", "posted_at": None, "description": text}
    ok, reason, *_ = prefilter.check(job, search_config(), 0)
    assert not ok and "blocker" in reason


def test_only_offered_sponsorship_counts_as_a_signal():
    from app.sources.feeds import SPONSOR_OFFERED
    assert SPONSOR_OFFERED.search("Visa sponsorship is available for this role")
    assert not SPONSOR_OFFERED.search("We are unable to offer visa sponsorship")
