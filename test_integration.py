"""
Day 27 — Crisis prevention: all 7 scenarios from PRD §11 Part B.
No real Bright Data or Claude calls needed — uses fixtures and mocks.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import anthropic

from extractor import extract_all, extract_skills
from pipeline import attach_evidence, format_output, isolate_job_descriptions, rank_gaps

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VALID_POSTING = {
    "job_description": (
        "We need a Data Engineer with Python, SQL, dbt, Airflow, and Spark. "
        "Experience with Kafka and Snowflake required. Docker and Terraform a plus. "
        "Must have strong communication skills and ability to work in an agile team."
    ),
    "url": "https://linkedin.com/jobs/123",
}

POSTINGS_15 = [
    {
        "job_description": (
            f"Job {i}: requires Python, SQL, dbt, Airflow, Spark, Kafka, Snowflake, Docker. "
            "Minimum 3 years experience. Strong communication skills required."
        ),
        "url": f"https://linkedin.com/jobs/{i}",
    }
    for i in range(1, 16)
]


# ---------------------------------------------------------------------------
# Part A — isolate_job_descriptions
# ---------------------------------------------------------------------------

class TestIsolateJobDescriptions(unittest.TestCase):

    def test_valid_payload_returns_strings(self):
        result = isolate_job_descriptions(POSTINGS_15)
        self.assertEqual(len(result), 15)
        self.assertTrue(all(isinstance(s, str) for s in result))

    def test_drops_short_records(self):
        payload = [{"job_description": "short", "url": "x"}] + POSTINGS_15
        result = isolate_job_descriptions(payload)
        self.assertEqual(len(result), 15)

    def test_drops_none_records(self):
        payload = [{"job_description": None, "url": "x"}] + POSTINGS_15
        result = isolate_job_descriptions(payload)
        self.assertEqual(len(result), 15)

    def test_drops_missing_field(self):
        payload = [{"url": "x"}] + POSTINGS_15
        result = isolate_job_descriptions(payload)
        self.assertEqual(len(result), 15)


# ---------------------------------------------------------------------------
# Part B — rank_gaps
# ---------------------------------------------------------------------------

class TestRankGaps(unittest.TestCase):

    def test_ranks_top_5_by_frequency(self):
        skills = [["python", "dbt", "airflow", "spark", "kafka", "snowflake"]] * 10
        user = "python, sql"
        result = rank_gaps(skills, user)
        self.assertEqual(len(result), 5)
        # All returned skills should not be in user set
        user_set = {"python", "sql"}
        for skill, _ in result:
            self.assertNotIn(skill, user_set)

    def test_returns_empty_when_user_covers_all(self):
        skills = [["python", "sql"]] * 5
        user = "python, sql"
        result = rank_gaps(skills, user)
        self.assertEqual(result, [])

    def test_case_insensitive(self):
        skills = [["Python", "DBT", "Airflow"]] * 5
        user = "python"
        result = rank_gaps(skills, user)
        skill_names = [s for s, _ in result]
        self.assertNotIn("python", skill_names)
        self.assertIn("dbt", skill_names)


# ---------------------------------------------------------------------------
# Part C — attach_evidence
# ---------------------------------------------------------------------------

class TestAttachEvidence(unittest.TestCase):

    def test_attaches_up_to_3_urls(self):
        gaps = [("dbt", 10), ("airflow", 8)]
        extracted = [["dbt", "airflow"]] * 15
        result = attach_evidence(gaps, POSTINGS_15, extracted)
        self.assertEqual(len(result), 2)
        self.assertLessEqual(len(result[0]["urls"]), 3)
        self.assertEqual(result[0]["skill"], "dbt")

    def test_fewer_than_3_urls_no_crash(self):
        gaps = [("dbt", 2)]
        postings = POSTINGS_15[:2]
        extracted = [["dbt"]] * 2
        result = attach_evidence(gaps, postings, extracted)
        self.assertEqual(len(result[0]["urls"]), 2)


# ---------------------------------------------------------------------------
# Crisis scenarios (PRD §11 Part B — all 7)
# ---------------------------------------------------------------------------

class TestCrisisScenarios(unittest.TestCase):

    # Scenario 1 — Empty Bright Data payload
    def test_empty_payload_raises(self):
        with self.assertRaises(ValueError) as ctx:
            isolate_job_descriptions([])
        self.assertIn("minimum 3 required", str(ctx.exception))

    # Scenario 2 — All job_description fields missing
    def test_all_missing_fields_raises(self):
        payload = [{"url": f"https://x.com/{i}"} for i in range(10)]
        with self.assertRaises(ValueError) as ctx:
            isolate_job_descriptions(payload)
        self.assertIn("minimum 3 required", str(ctx.exception))

    # Scenario 3 — 1/20 Claude calls return malformed JSON
    def test_malformed_json_skipped(self):
        async def run():
            responses = ["not json"] + [
                '{"skills": ["Python", "SQL"]}' for _ in range(19)
            ]
            call_count = 0

            async def mock_create(**kwargs):
                nonlocal call_count
                text = responses[call_count % len(responses)]
                call_count += 1
                mock_resp = AsyncMock()
                mock_resp.content = [AsyncMock(text=text)]
                return mock_resp

            client = AsyncMock(spec=anthropic.AsyncAnthropic)
            client.messages.create = mock_create

            jds = ["valid job description with enough text to pass the 50 char check"] * 20
            results = await extract_all(client, jds)
            empty = sum(1 for r in results if r == [])
            non_empty = sum(1 for r in results if r != [])
            return empty, non_empty

        empty, non_empty = asyncio.run(run())
        self.assertEqual(empty, 1)
        self.assertEqual(non_empty, 19)

    # Scenario 4 — 3/20 calls hit asyncio.TimeoutError
    def test_timeout_skipped(self):
        async def run():
            call_count = 0

            async def mock_create(**kwargs):
                nonlocal call_count
                call_count += 1
                if call_count <= 3:
                    raise asyncio.TimeoutError()
                mock_resp = AsyncMock()
                mock_resp.content = [AsyncMock(text='{"skills": ["Python"]}')]
                return mock_resp

            client = AsyncMock(spec=anthropic.AsyncAnthropic)
            client.messages.create = mock_create

            jds = ["valid job description with enough characters to pass the guard"] * 20
            results = await extract_all(client, jds)
            empty = sum(1 for r in results if r == [])
            return empty

        empty = asyncio.run(run())
        self.assertEqual(empty, 3)

    # Scenario 5 — RateLimitError on first call → retry once → skip on second failure
    def test_rate_limit_retry_then_skip(self):
        async def run():
            call_count = 0

            async def mock_create(**kwargs):
                nonlocal call_count
                call_count += 1
                raise anthropic.RateLimitError(
                    "rate limit",
                    response=AsyncMock(status_code=429),
                    body={},
                )

            client = AsyncMock(spec=anthropic.AsyncAnthropic)
            client.messages.create = mock_create

            with patch("extractor.asyncio.sleep", new=AsyncMock()):
                result = await extract_skills(client, "valid job description with enough text here")

            return result, call_count

        result, call_count = asyncio.run(run())
        self.assertEqual(result, [])
        self.assertEqual(call_count, 2)  # initial + 1 retry

    # Scenario 6 — Bright Data returns 2 postings
    def test_two_postings_raises(self):
        payload = [
            {"job_description": "x" * 60, "url": f"https://x.com/{i}"}
            for i in range(2)
        ]
        with self.assertRaises(ValueError) as ctx:
            isolate_job_descriptions(payload)
        self.assertIn("minimum 3 required", str(ctx.exception))

    # Scenario 7 — User skills superset → 0 gaps → empty list, no crash
    def test_user_superset_returns_empty(self):
        skills = [["python", "sql"]] * 5
        user = "python, sql, dbt, airflow, spark, kafka"
        result = rank_gaps(skills, user)
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# format_output edge cases
# ---------------------------------------------------------------------------

class TestFormatOutput(unittest.TestCase):

    def test_no_gaps(self):
        out = format_output([], total_postings=15)
        self.assertIn("No skill gaps", out)

    def test_fewer_than_3_urls_no_crash(self):
        evidence = [{"skill": "dbt", "count": 5, "urls": ["https://x.com/1"]}]
        out = format_output(evidence, total_postings=15)
        self.assertIn("GAP #1: dbt", out)
        self.assertIn("https://x.com/1", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
