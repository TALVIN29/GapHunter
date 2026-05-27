"""
GapHunter — Career Intel Agent
Gradio UI — Day 28
PRD §5: Flat Harness. Single-page, 3 inputs, 1 output.
"""

import asyncio
import time
import traceback

import anthropic
import gradio as gr
from dotenv import load_dotenv

from extractor import extract_all
from pipeline import (
    attach_evidence,
    format_output,
    isolate_job_descriptions,
    rank_gaps,
)
from scraper import scrape_jobs

load_dotenv()


def pipeline(user_skills: str, job_role: str, location: str) -> str:
    """
    Single Gradio-callable function. Synchronous wrapper over async pipeline.
    PRD §5: explicit steps, no framework, no hidden state.
    Returns formatted string — gaps or user-readable error. No stack traces.
    """
    # --- Input validation ---
    if not user_skills.strip():
        return "Please enter your skills (comma-separated, e.g. Python, SQL, pandas)."
    if not job_role.strip():
        return "Please enter a target job role (e.g. Data Analyst, ML Engineer)."
    if not location.strip():
        location = "United States"

    t_start = time.perf_counter()

    try:
        # Step 1 — Scrape
        postings = scrape_jobs(job_role.strip(), location.strip())
    except EnvironmentError as e:
        return f"Configuration error: {e}"
    except Exception:
        return (
            "Could not retrieve job listings from LinkedIn. "
            "This may be a temporary issue — please try again in a few seconds."
        )

    try:
        # Step 2 — Isolate quality job descriptions
        job_descriptions = isolate_job_descriptions(postings)
    except ValueError as e:
        return f"Not enough job listings found: {e}\nTry a broader job role or location."

    # Step 3 — Extract skills (async concurrent Claude calls)
    client = anthropic.AsyncAnthropic()
    all_skills = asyncio.run(extract_all(client, job_descriptions))

    # Step 4 — Rank gaps
    gaps = rank_gaps(all_skills, user_skills)

    elapsed = time.perf_counter() - t_start

    if not gaps:
        return (
            f"No skill gaps found across {len(postings)} live job postings.\n"
            f"Your skills already match what employers are asking for in '{job_role}'.\n"
            f"Completed in {elapsed:.1f}s."
        )

    # Step 5 — Attach evidence URLs
    evidence = attach_evidence(gaps, postings, all_skills)

    output = format_output(evidence, total_postings=len(postings))
    output += f"\n\nScanned {len(postings)} live LinkedIn postings in {elapsed:.1f}s."
    return output


# ---------------------------------------------------------------------------
# Gradio interface — PRD §4: 2 required inputs + 1 optional + 1 output
# ---------------------------------------------------------------------------

with gr.Blocks(title="GapHunter — Career Intel Agent") as demo:
    gr.Markdown(
        """
# GapHunter
### Find the skills blocking you from getting hired — using live job data.

Enter your current skills and target role. GapHunter scans live LinkedIn job postings
and returns the top skill gaps ranked by how often employers require them.

*No career coach fluff. One query. Live data.*
        """
    )

    with gr.Row():
        with gr.Column():
            skills_input = gr.Textbox(
                label="Your Current Skills",
                placeholder="Python, SQL, pandas, scikit-learn",
                lines=2,
            )
            role_input = gr.Textbox(
                label="Target Job Role",
                placeholder="Data Analyst",
                lines=1,
            )
            location_input = gr.Textbox(
                label="Location (optional)",
                placeholder="United States",
                value="United States",
                lines=1,
            )
            run_btn = gr.Button("Find My Skill Gaps", variant="primary")

        with gr.Column():
            output = gr.Textbox(
                label="Your Skill Gap Report",
                lines=20,
            )

    gr.Markdown(
        """
---
*Powered by [Bright Data](https://brightdata.com) + Claude Sonnet 4.6 |
Built for the Bright Data AI Agents Hackathon 2026*
        """
    )

    run_btn.click(
        fn=pipeline,
        inputs=[skills_input, role_input, location_input],
        outputs=output,
    )

if __name__ == "__main__":
    demo.launch(show_error=False)
