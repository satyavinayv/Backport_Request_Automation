# 🚀 Backport Request Automation

> **Stop doing backports manually. Let the machine do it.**

This tool automates the entire GitLab/Jira/GM2 backport workflow — from GM2 eligibility validation to branch creation, cherry-picking, Jira comments, and workflow transitions — all in a single command.

> 📊 **Manager demo:** Open [`demo.html`](./demo.html) in any browser for a 10-slide visual walkthrough of the tool.

---

## ⚡ What Happens When You Run This?

```powershell
python backport.py --mr 91281 --execute
```

Or combine multiple MRs into one backport:
```powershell
python backport.py --mr 93015 93011 --execute
```

In under 60 seconds, the script will:

1. **Extract** Jira ID, author, and merge timestamp from the MR
2. **Resolve Test IDs** using a 4-phase cascade (see below)
3. **Fetch** Fix Version(s) from Jira automatically
4. **Query OpenSearch** for GM2 execution results post-merge
5. **Evaluate** GM2 eligibility using strict consecutive PASS rules
6. **Create** backport branch(es) from the release branch
7. **Cherry-pick** commits from develop onto the backport branch
8. **Open** a Backport MR in GitLab with assignee, reviewers, and labels set automatically
9. **Post** the full approval comment to Jira with dynamic GM2 links
10. **Transition** Jira workflow: `Running on GM2` → `GM Data Creation` → `MR to GM`

**All of this. One command.**

---

## Test ID Extraction — 4-Phase Cascade

The script uses a cascade to find TC/Xray IDs, trying each phase before falling back:

| Phase | Source | Triggers when |
|---|---|---|
| **Phase 1** | `+` lines in the git diff | Always tried first |
| **Phase 2** | Scenario block isolation in modified test files | Phase 1 returns empty |
| **Fallback** | `Test Cases:` / `Xray IDs:` lines in MR description | Phases 1 & 2 return empty |
| **Phase 3** | Jira description → OpenSearch `scenario` field lookup | All above return empty |

### Phase 3 Detail

Phase 3 is triggered when only non-test files are modified (e.g., helper classes). It:
1. Parses the Jira ticket description for scenario names — supports two formats:
   - **Structured labels**: `Scenario: <name>`, `Feature: <name>`, `TC ID: <id>`
   - **Pipeline failure report table**: tab-separated with `Scenario/Method` column
2. Queries OpenSearch with `match_phrase` on the `scenario` field
3. Collects `test_case_id` / `x_ray_id` from hits as the resolved IDs

---

## GM2 Eligibility Rules

Only runs where `rerun_count = 0` and `isCBB = false` are counted.
Up to 50 qualifying runs are fetched; only the **2 most recent** determine eligibility.

| Run Pattern | Eligible? |
|---|---|
| ✅ PASS → PASS | **YES** — trigger immediately |
| ✅ FAIL → PASS → PASS | **YES** |
| ❌ PASS → FAIL → PASS | **NO** — preceding run failed |
| ❌ PASS → PASS → FAIL | **NO** — latest run failed |
| ❌ Less than 2 runs | **NO** — insufficient data |

> **Note:** All test cases in the MR must be eligible. One failure = DO NOT BACKPORT — unless that test is selectively bypassed using `--bypass-gm2-for` or the interactive prompt (with a recorded reason).

---

## 🔄 Jira Workflow Transitions (Auto)

| From | To | Trigger |
|---|---|---|
| `Running on GM2` | `GM Data Creation` | Detected by script |
| `GM Data Creation` | `MR to GM` | Chained immediately |
| `MR to GM` | — | Already done, skipped |

---

## 🛠️ Prerequisites

### 1. Python 3.8+
```powershell
python --version
```

### 2. Install Dependencies
```powershell
pip install requests pytest
```

### 3. Company VPN
You must be connected to the company VPN. OpenSearch (`autoinfra-es.vaultdev.com`) is not accessible without it.

### 4. Access Tokens

| Token | Where to get it |
|---|---|
| **GitLab PAT** | `https://gitlab.veevadev.com/-/profile/personal_access_tokens` |
| **Jira PAT** | `https://jira.veevadev.com` → Profile → Personal Access Tokens |

*GitLab token scopes required:* `api`, `read_api`, `read_repository`, `write_repository`

---

## ⚙️ Setup (Do This Once)

### Step 1 — Clone or download the repo
```powershell
cd C:\Working_Env
git clone <repo-url> BackportRequestAutomation
cd BackportRequestAutomation
```

### Step 2 — Create your credentials file
```powershell
copy tools\backport\.env.example tools\backport\set_env.ps1
```

Edit `set_env.ps1`:
```powershell
$env:GITLAB_TOKEN      = 'your-gitlab-pat-here'
$env:GITLAB_URL        = 'https://gitlab.veevadev.com'
$env:JIRA_URL          = 'https://jira.veevadev.com'
$env:JIRA_PAT          = 'your-jira-pat-here'
$env:OPENSEARCH_URL    = 'https://autoinfra-es.vaultdev.com:9200'

# Optional — auto-add these reviewers to every backport MR
$env:BACKPORT_REVIEWERS = 'john.doe,jane.smith'

# Optional — add these labels to every backport MR (alongside original MR labels + "backport")
$env:BACKPORT_LABELS   = 'team-qa'
```

🔒 `set_env.ps1` is in `.gitignore` — it will never be committed. Never share this file or paste its contents anywhere.

### Step 3 — Load credentials
```powershell
cd tools\backport
. .\set_env.ps1
```

### Step 4 — Verify setup
```powershell
python backport.py --mr 91281
```
If you see the eligibility report — you're good to go. ✅

---

## 📝 Usage

Navigate to the script directory and load credentials:
```powershell
cd C:\Working_Env\BackportRequestAutomation\tools\backport
. .\set_env.ps1
```

### 🔍 Phase 1 — Dry Run (Read-Only, Safe to Run Anytime)
```powershell
python backport.py --mr <MR_IID>
```

What it does:
- Fetches MR metadata and resolves test IDs (all 4 phases)
- Checks Jira Fix Version(s)
- Queries GM2 results and evaluates eligibility
- Lists commits to cherry-pick
- Checks for existing backport MRs
- Makes **zero changes** anywhere

Example output:
```
============================================================
  BACKPORT ELIGIBILITY REPORT
============================================================

MR:            !91281
Title:         QA-558547 Script Update for GM and GM2 Failure
Author:        satyavinay.vella
Merged At:     2026-08-26T11:55:40.941Z
Jira:          QA-558547
ID Source:     DIFF
Xray IDs:      DEV-1085503

Fix Versions found: 26R2.2, 26R2.3

Test Case GM2 Results:
----------------------------------------
  DEV-1085503: Passed -> Passed -> Passed -> [ELIGIBLE]
----------------------------------------

Overall: BACKPORT

Commits to cherry-pick (2):
  a4f1bece - QA-558547 Script Update for GM Failure
  11e1ab76 - QA-558547 Script Update

Versions to process: 2

  Fix Version:     26R2.2
  Target Branch:   release/26.2.2
  Backport Branch: r26.2.2_gm/satyavinay.vella/QA-558547_SU

  Fix Version:     26R2.3
  Target Branch:   release/26.2.3
  Backport Branch: r26.2.3_gm/satyavinay.vella/QA-558547_SU

Dry run complete. Run with --execute to create the backport MR and post Jira comment.
============================================================
```

### Inspect Test IDs
```powershell
python backport.py --mr <MR_IID> --inspect-ids
```
Shows exactly which test IDs were found, which phase found them, and which lines/blocks they came from. Safe to run anytime.

### 🚀 Phase 2 — Full Execution
```powershell
python backport.py --mr <MR_IID> --execute
```

Only run this after Phase 1 shows `Overall: BACKPORT`.

What it does (in addition to Phase 1):
- Creates backport branch(es) from the release branch
- Cherry-picks commits (stops and cleans up on conflict)
- Opens Backport MR in GitLab with assignee, reviewers, and labels set
- Posts approval comment to Jira with GM2 run links
- Transitions Jira workflow automatically

### Custom Reviewers and Labels
```powershell
python backport.py --mr <MR_IID> --execute --reviewers john.doe jane.smith --labels urgent,needs-review
```

**Reviewers** — merged from three sources (all combined, no duplicates):
1. `BACKPORT_REVIEWERS` env var (set once in `set_env.ps1`)
2. `--reviewers` CLI flag (per-run override)

**Labels** — merged from four sources (all combined, no duplicates):
1. Original develop MR labels (copied automatically)
2. Fixed `backport` label (always added)
3. `BACKPORT_LABELS` env var (set once in `set_env.ps1`)
4. `--labels` CLI flag (per-run override)

### Override Target Branch
```powershell
python backport.py --mr <MR_IID> --execute --target-branch release/26.2.2
```
If the override doesn't match the Jira Fix Version, the script will warn and ask for confirmation.

### CI/CD Headless Mode
```powershell
python backport.py --mr <MR_IID> --execute --non-interactive
```
Fails fast on any confirmation prompt — safe for pipeline use.

### ⚠️ GM2 Bypass Options

#### Global Bypass — Skip All GM2 Checks
Use when the entire GM2 pipeline is broken (smoke failure, infra issue) and the backport must go through regardless:
```powershell
python backport.py --mr <MR_IID> --execute --bypass-gm2
python backport.py --mr <MR_IID> --execute --bypass-gm2 --bypass-gm2-reason "Smoke failure blocking release"
```
This skips GM2 validation for **all** test cases. The bypass reason is recorded in the Jira comment.

#### Selective Bypass — Skip Only Specific Failing Test(s)
Use when **one test** is failing for a known, unrelated reason (flaky test, environment glitch) while other tests should still be validated normally:
```powershell
# Pre-declare which test IDs to bypass (non-interactive / CI-safe)
python backport.py --mr <MR_IID> --execute --bypass-gm2-for TC-1234 DEV-5678

# With an explicit reason (recorded in terminal output + Jira comment)
python backport.py --mr <MR_IID> --execute \
    --bypass-gm2-for TC-1234 \
    --bypass-gm2-for-reason "Known flaky test, failure unrelated to this fix"
```

**Interactive mode (no flag needed):** When running in a terminal and a test fails, the script pauses and prompts you:
```
  TC-1234: Passed -> Failed -> [NOT ELIGIBLE]
    Reason: Latest two runs are not both PASS: passed -> failed
    Bypass GM2 check for TC-1234? (y/n): y
    Enter bypass reason for TC-1234: Known flaky test — environment issue on GM2, unrelated to fix
    [BYPASSED] TC-1234: Known flaky test — environment issue on GM2, unrelated to fix
```
If you answer `n`, the TC remains ineligible and execution is blocked. The bypass reason is embedded in the Jira comment's GM2 section.

| Flag | Scope | When to use |
|---|---|---|
| `--bypass-gm2` | All TCs | Infra/smoke failure — nothing can run |
| `--bypass-gm2-for <ID> ...` | Specific TCs | One test is known-flaky / unrelated failure |
| Interactive prompt | One TC at a time | You see a failure and want to decide on the spot |

---

## MR Description Format (Optional)

If test IDs are not in the code diffs, you can add them to the MR description as a fallback:
```text
Test Cases: TC-1234, TC-5678
Xray IDs: DEV-1085503
```

If neither the code diffs nor the description contain IDs, the script automatically falls back to Phase 3 (Jira description scenario lookup).

---

## 🚨 Error Scenarios & What To Do

| Error | Cause | Fix |
|---|---|---|
| Missing required environment variables | `set_env.ps1` not loaded | Run `. .\set_env.ps1` |
| Cannot reach OpenSearch | Not on VPN | Connect to company VPN |
| No Jira ID found in MR title | MR title missing QA-XXXXXX | Add Jira ID to MR title |
| No Test Cases or Xray IDs found | All 4 extraction phases failed | Add `Xray IDs: DEV-XXXXXX` to MR description |
| No Fix Version set in Jira | Jira ticket missing Fix Version | Set Fix Version in Jira before running |
| Cherry-pick conflict | Changes conflict with release branch | Resolve manually; branch auto-deleted |
| DO NOT BACKPORT | GM2 runs don't meet eligibility | Wait for more GM2 runs and re-run, OR use `--bypass-gm2-for <TC_ID>` if the failure is known/unrelated |
| Backport MR already exists | Already created | Script skips creation, posts comment only |
| GitLab user not found | Wrong username in --reviewers | Check username spelling in GitLab |

---

## 🔁 Day-to-Day Workflow

```
Developer merges MR into develop
         ↓
GM2 executions run automatically
         ↓
Run: python backport.py --mr <IID>
         ↓
Check: Overall: BACKPORT?
    YES ──────────────────────────────────────────────────────┐
    NO  → Any test failing for a known/unrelated reason?      │
              YES → Interactive: script prompts per failing TC │
                    OR use: --bypass-gm2-for <TC_ID>          │
                    Bypass reason recorded in Jira comment     │
              NO  → Wait for more GM2 runs, re-run Phase 1    │
         ↓ ◄──────────────────────────────────────────────────┘
python backport.py --mr <IID> --execute
         ↓
Backport MR created in GitLab ✅
  - Assigned to script runner
  - Reviewers set automatically
  - Labels: original + "backport"
Jira comment posted ✅
  - GM2 dashboard links per test case
  - Bypass reasons shown for any bypassed TCs
Jira workflow transitioned ✅
  Running on GM2 → GM Data Creation → MR to GM
         ↓
Manager approves Jira comment
         ↓
Backport MR merged into release branch ✅
```

---

## Running Tests

```powershell
cd C:\Working_Env\BackportRequestAutomation\tools\backport
python -m pytest tests/ -v
```

Run a specific test file:
```powershell
python -m pytest tests/test_diff_scanner.py -v
```

Run with coverage:
```powershell
pip install pytest-cov
python -m pytest tests/ --cov=. --cov-report=term-missing -v
```

Tests are fully offline — no VPN, no real API calls. All external calls are mocked.

---

## 📁 Project Structure

```
tools/backport/
│
├── backport.py                            ← CLI entry point + orchestration
├── config.py                              ← env vars & constants
│
├── clients/                               ← External API HTTP layers
│   ├── gitlab.py                          ← GitLab REST (get/post/raw, user lookup)
│   ├── jira.py                            ← Jira REST (get/post comment)
│   └── opensearch.py                      ← OpenSearch queries
│
├── features/                              ← Business logic by feature
│   ├── id_extraction/
│   │   ├── __init__.py                    ← Resolver: coordinates all 4 phases
│   │   ├── diff_scanner.py                ← Phase 1: scan +lines in git diff
│   │   ├── scenario_parser.py             ← Phase 2: scenario block isolation
│   │   └── jira_test_linker.py            ← Phase 3: Jira description → OpenSearch lookup
│   ├── gm2_eligibility/
│   │   ├── runner.py                      ← Fetch GM2 runs from OpenSearch
│   │   └── evaluator.py                   ← 2x consecutive PASS eligibility rule
│   ├── backport/
│   │   ├── branch_ops.py                  ← Create branch, cherry-pick, delete
│   │   └── mr_ops.py                      ← Check/create MR, build label set
│   └── jira_workflow/
│       ├── issue.py                       ← Fix versions, caused-by, parse_fix_version
│       ├── transitions.py                 ← Resolve & apply status transitions
│       └── comment_builder.py             ← Build the approval comment
│
├── utils/                                 ← Shared helpers
│   ├── log.py                             ← err / warn / info
│   └── dashboard_url.py                   ← Build & shorten OpenSearch dashboard URLs
│
├── tests/                                 ← Offline unit tests (no API calls)
│   ├── conftest.py
│   ├── test_diff_scanner.py
│   ├── test_scenario_parser.py
│   ├── test_gm2_evaluator.py
│   ├── test_gm2_runner.py
│   ├── test_id_extraction.py
│   ├── test_jira_test_linker.py
│   ├── test_mr_ops.py
│   ├── test_jira_issue.py
│   └── test_comment_builder.py
│
├── .env.example                           ← Credentials template (safe to commit)
├── set_env.ps1                            ← Your credentials (NEVER commit this)
└── demo.html                              ← Manager-facing slide deck (open in any browser)
```

---

## 🚨 Security Rules

- `set_env.ps1` is in `.gitignore` — never committed
- `.env` is in `.gitignore` — never committed
- Never paste tokens in MR descriptions, comments, or chat
- Never commit credentials in any file
- Never share `set_env.ps1` with anyone — each user creates their own

---

## 👥 Onboarding a New Team Member

1. Share this repo link
2. Ask them to generate their own GitLab PAT and Jira PAT
3. They follow Setup Steps 1–4 above
4. Optionally set `BACKPORT_REVIEWERS` and `BACKPORT_LABELS` in their `set_env.ps1`
5. Done — they're ready to run backports

---

## 🆘 Need Help?

| Problem | Contact                         |
|---|---------------------------------|
| Script errors | Check error table above first   |
| GitLab token issues | Your GitLab Profile             |
| Jira access issues | Your Jira Profile               |
| OpenSearch / GM2 data issues | QA Infrastructure team          |
| Any Other Issue | Contact Me: SatyaVinay.Vella 🫡 |