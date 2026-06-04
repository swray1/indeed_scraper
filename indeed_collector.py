from __future__ import annotations

import argparse
import csv
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urljoin

import httpx
import yaml
from bs4 import BeautifulSoup


INDEED_BASE_URL = "https://www.indeed.com"
PLACEHOLDER_JOB_KEYS = {"abcdef0123456789", "fedcba9876543210"}


@dataclass
class JobPosting:
    position: str
    company: str
    salary: str
    job_title: str
    job_description: str
    location: str
    job_url: str
    source: str
    scraped_at: str
    matched_icp: str = ""
    match_reasons: str = ""


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def build_search_url(position: str, location: str, radius: int, page: int) -> str:
    start = page * 10
    return (
        f"{INDEED_BASE_URL}/jobs"
        f"?q={quote_plus(position)}"
        f"&l={quote_plus(location)}"
        f"&radius={radius}"
        f"&start={start}"
    )


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def first_text(card: BeautifulSoup, selectors: list[str]) -> str:
    for selector in selectors:
        node = card.select_one(selector)
        text = clean_text(node.get_text(" ", strip=True) if node else "")
        if text:
            return text
    return ""


def extract_job_key(card: BeautifulSoup) -> str:
    for attr in ("data-jk", "data-jobkey"):
        value = card.get(attr)
        if value:
            return value
    link = card.select_one("a[href*='jk=']")
    if not link:
        return ""
    match = re.search(r"[?&]jk=([^&]+)", link.get("href", ""))
    return match.group(1) if match else ""


def extract_job_key_from_url(url: str) -> str:
    match = re.search(r"[?&]jk=([^&]+)", url)
    return match.group(1) if match else ""


def extract_job_url(card: BeautifulSoup) -> str:
    link = card.select_one("h2.jobTitle a[href], a.jcs-JobTitle[href], a[href*='jk=']")
    if not link:
        return ""
    href = link.get("href", "")
    return urljoin(INDEED_BASE_URL, href)


def parse_search_page(html: str, position: str, scraped_at: str) -> list[JobPosting]:
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("div.job_seen_beacon")
    if not cards:
        cards = soup.select("td.resultContent, div.result")
    postings: list[JobPosting] = []
    seen: set[str] = set()

    for card in cards:
        job_key = extract_job_key(card)
        if job_key.lower() in PLACEHOLDER_JOB_KEYS:
            continue
        job_url = extract_job_url(card)
        unique_key = job_key or job_url
        if unique_key and unique_key in seen:
            continue
        if unique_key:
            seen.add(unique_key)

        job_title = first_text(
            card,
            [
                "h2.jobTitle span[title]",
                "h2.jobTitle span",
                "[data-testid='job-title']",
                ".jobTitle",
            ],
        )
        company = first_text(
            card,
            [
                "[data-testid='company-name']",
                ".companyName",
                "span.companyName",
            ],
        )
        if company and (company == job_title or company.endswith("...")):
            company = ""
        location = first_text(
            card,
            [
                "[data-testid='text-location']",
                ".companyLocation",
                "div.companyLocation",
            ],
        )
        salary = first_text(
            card,
            [
                "[data-testid='attribute_snippet_testid']",
                ".salary-snippet-container",
                ".metadata.salary-snippet-container",
            ],
        )
        description = first_text(
            card,
            [
                ".job-snippet",
                "[data-testid='jobsnippet_footer']",
                ".summary",
            ],
        )

        if not job_title and not company:
            continue

        postings.append(
            JobPosting(
                position=position,
                company=company,
                salary=salary,
                job_title=job_title,
                job_description=description,
                location=location,
                job_url=job_url,
                source="indeed",
                scraped_at=scraped_at,
            )
        )

    return postings


def fetch_search(client: httpx.Client, url: str) -> str:
    response = client.get(url, follow_redirects=True)
    response.raise_for_status()
    return response.text


def collect_jobs(config: dict[str, Any]) -> list[JobPosting]:
    request_config = config.get("request", {})
    headers = {"User-Agent": request_config.get("user_agent", "Mozilla/5.0")}
    timeout = request_config.get("timeout_seconds", 25)
    delay = request_config.get("delay_seconds", 3)
    scraped_at = datetime.now(timezone.utc).isoformat()
    postings: list[JobPosting] = []

    with httpx.Client(headers=headers, timeout=timeout) as client:
        for search in config.get("searches", []):
            position = str(search["position"])
            location = str(search.get("location", "United States"))
            radius = int(search.get("radius", 25))
            max_pages = int(search.get("max_pages", 1))

            for page in range(max_pages):
                url = build_search_url(position, location, radius, page)
                try:
                    html = fetch_search(client, url)
                except httpx.HTTPError as exc:
                    print(f"Could not fetch {url}: {exc}")
                    continue

                postings.extend(parse_search_page(html, position, scraped_at))
                time.sleep(delay)

    return dedupe_postings(postings)


def dedupe_postings(postings: list[JobPosting]) -> list[JobPosting]:
    deduped: list[JobPosting] = []
    seen: set[tuple[str, str, str]] = set()

    for posting in postings:
        job_key = extract_job_key_from_url(posting.job_url)
        key = (
            (job_key or posting.job_url).lower(),
            posting.company.lower(),
            posting.job_title.lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(posting)

    return deduped


def match_icp(posting: JobPosting, profiles: list[dict[str, Any]]) -> JobPosting:
    haystack = " ".join(
        [
            posting.position,
            posting.company,
            posting.job_title,
            posting.job_description,
            posting.location,
        ]
    ).lower()
    matched_profiles: list[str] = []
    reasons: list[str] = []

    for profile in profiles:
        name = str(profile.get("name", "Unnamed ICP"))
        include_keywords = [str(item).lower() for item in profile.get("include_keywords", [])]
        exclude_keywords = [str(item).lower() for item in profile.get("exclude_keywords", [])]

        excludes = [keyword for keyword in exclude_keywords if keyword in haystack]
        if excludes:
            continue

        includes = [keyword for keyword in include_keywords if keyword in haystack]
        if includes:
            matched_profiles.append(name)
            reasons.append(f"{name}: {', '.join(includes)}")

    posting.matched_icp = "; ".join(matched_profiles)
    posting.match_reasons = "; ".join(reasons)
    return posting


def apply_icp_filters(postings: list[JobPosting], profiles: list[dict[str, Any]]) -> list[JobPosting]:
    filtered: list[JobPosting] = []
    for posting in postings:
        match_icp(posting, profiles)
        if posting.matched_icp:
            filtered.append(posting)
    return filtered


def write_csv(path: Path, postings: list[JobPosting]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(JobPosting.__dataclass_fields__.keys())
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for posting in postings:
            writer.writerow(asdict(posting))


def write_json(path: Path, postings: list[JobPosting]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump([asdict(posting) for posting in postings], handle, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect Indeed job postings and export CRM-ready files.")
    parser.add_argument("--config", default="config.yaml", help="Path to config YAML.")
    args = parser.parse_args()

    config = load_config(Path(args.config))
    output_dir = Path(config.get("output", {}).get("directory", "output"))
    postings = collect_jobs(config)
    filtered = apply_icp_filters(postings, config.get("icp_profiles", []))

    write_csv(output_dir / "jobs_raw.csv", postings)
    write_csv(output_dir / "jobs_filtered.csv", filtered)
    write_json(output_dir / "jobs_raw.json", postings)

    print(f"Collected {len(postings)} postings.")
    print(f"Matched {len(filtered)} postings to ICP profiles.")
    print(f"Wrote output to {output_dir.resolve()}.")


if __name__ == "__main__":
    main()
