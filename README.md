# Indeed Job Posting Collector

Collect job postings from Indeed searches, normalize them into CRM-friendly rows, and split the output by ICP/account profile.

## What It Produces

Each run writes:

- `output/jobs_raw.csv`: all collected postings
- `output/jobs_filtered.csv`: postings that match at least one ICP profile
- `output/jobs_raw.json`: raw structured records for debugging or later imports

Fields:

- `position`
- `company`
- `salary`
- `job_title`
- `job_description`
- `location`
- `job_url`
- `source`
- `scraped_at`
- `matched_icp`
- `match_reasons`

## Setup

1. Install Python 3.11 or newer.
2. Install dependencies:

```powershell
python -m pip install -r requirements.txt
```

3. Copy the example config:

```powershell
Copy-Item config.example.yaml config.yaml
```

4. Edit `config.yaml` with the positions, locations, and ICP keywords you want.

## Run

```powershell
python .\indeed_collector.py --config .\config.yaml
```

## Notes

This collector uses normal search-result pages and polite pacing. It does not bypass login walls, bot checks, paywalls, or access controls. If Indeed blocks a request, the run records what it can and continues.

