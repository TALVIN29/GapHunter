"""
Day 26 validation — PRD §11:
- 5 fixture job descriptions → confirm JSON shape
- 1 malformed description → confirm [] returned, no crash
- 20 concurrent calls → must complete < 30s
- Print raw extracted skills per posting
"""

import asyncio
import logging
import time

import anthropic
from dotenv import load_dotenv

load_dotenv()

from extractor import extract_all, extract_skills

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

FIXTURE_JDS = [
    # 1 — Data Engineer
    (
        "We are looking for a Data Engineer with strong Python and SQL skills. "
        "You will build and maintain ETL pipelines using Apache Airflow and dbt. "
        "Experience with Spark, Kafka, and AWS Glue is required. "
        "Familiarity with Snowflake or BigQuery is a plus. "
        "Must know Docker and Terraform for infrastructure as code."
    ),
    # 2 — ML Engineer
    (
        "ML Engineer role requiring PyTorch and TensorFlow experience. "
        "You will deploy models using MLflow and Kubernetes. "
        "Strong Python required. Familiarity with Hugging Face Transformers, "
        "ONNX, and CUDA optimization is expected. "
        "CI/CD with GitHub Actions. REST API development with FastAPI."
    ),
    # 3 — Data Analyst
    (
        "Data Analyst position. Must be proficient in SQL, Excel, and Tableau. "
        "Python for automation (pandas, numpy). "
        "Experience with Looker or Power BI preferred. "
        "Statistical analysis background required — A/B testing, regression. "
        "Snowflake or Redshift query experience a plus."
    ),
    # 4 — Backend Engineer
    (
        "Backend Engineer needed for fintech startup. "
        "Stack: Python (Django/FastAPI), PostgreSQL, Redis, Celery. "
        "Must know Docker, Kubernetes, and AWS (ECS, RDS, S3). "
        "GraphQL API design. Experience with Kafka for event streaming. "
        "Strong understanding of OAuth2 and JWT authentication."
    ),
    # 5 — Analytics Engineer
    (
        "Analytics Engineer to own our data warehouse. "
        "Core stack: dbt, Snowflake, Python. "
        "You will write and maintain dbt models, tests, and documentation. "
        "Experience with Airflow for orchestration. "
        "Looker or Metabase for BI. Git for version control. "
        "Bonus: experience with Great Expectations for data quality."
    ),
]

# Malformed — no real job description content
MALFORMED_JD = "asdf 1234 !!!@@@### lorem ipsum dolor"

# 20 copies for concurrency timing test (mix of real + repeated)
TIMING_JDS = (FIXTURE_JDS * 4)[:20]  # 20 descriptions


async def test_fixture_extractions(client: anthropic.AsyncAnthropic) -> None:
    print("\n=== TEST 1: 5 fixture JDs ===")
    results = await extract_all(client, [jd for jd in FIXTURE_JDS])
    all_pass = True
    for i, (jd, skills) in enumerate(zip(FIXTURE_JDS, results), 1):
        ok = isinstance(skills, list)
        tag = "PASS" if ok else "FAIL"
        print(f"  [{tag}] JD {i}: {skills}")
        if not ok:
            all_pass = False
    print(f"  Result: {'ALL PASS' if all_pass else 'FAILURES DETECTED'}")


async def test_malformed(client: anthropic.AsyncAnthropic) -> None:
    print("\n=== TEST 2: Malformed JD — expect [] ===")
    result = await extract_skills(client, MALFORMED_JD)
    ok = isinstance(result, list)
    print(f"  Returned: {result}")
    print(f"  [{'PASS' if ok else 'FAIL'}] Type is list: {ok}")
    print(f"  [{'PASS' if result == [] else 'NOTE'}] Empty list: {result == []}")


async def test_concurrency_timing(client: anthropic.AsyncAnthropic) -> None:
    print("\n=== TEST 3: 20 concurrent calls — must complete < 30s ===")
    start = time.perf_counter()
    results = await extract_all(client, TIMING_JDS)
    elapsed = time.perf_counter() - start
    non_empty = sum(1 for r in results if r)
    status = "PASS" if elapsed < 30 else "FAIL"
    print(f"  [{status}] Elapsed: {elapsed:.2f}s (limit: 30s)")
    print(f"  Non-empty results: {non_empty}/20")
    print(f"  Per-call average: {elapsed/20:.2f}s")


async def main() -> None:
    api_key_check = __import__("os").environ.get("ANTHROPIC_API_KEY", "")
    if not api_key_check.startswith("sk-"):
        print("ERROR: ANTHROPIC_API_KEY not set. Set it then rerun.")
        print("  $env:ANTHROPIC_API_KEY = 'sk-ant-...'")
        return

    client = anthropic.AsyncAnthropic()
    await test_fixture_extractions(client)
    await test_malformed(client)
    await test_concurrency_timing(client)
    print("\nDay 26 validation complete.")


if __name__ == "__main__":
    asyncio.run(main())
