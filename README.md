# 🚀 Backport Request Automation

> **Stop doing backports manually. Let the machine do it.**

This tool automates the entire GitLab/Jira/GM2 backport workflow — from GM2 eligibility validation to branch creation, cherry-picking, Jira comments, and workflow transitions — all in a single command.

---

## ⚡ What Happens When You Run This?

```powershell
python backport.py --mr 91281 --execute
```

In under 60 seconds, the script will:

1. 🔍 **Extract** Jira ID, Xray IDs, author, and merge timestamp from the MR
2. 📋 **Fetch** Fix Version(s) from Jira automatically
3. 🧪 **Query OpenSearch** for GM2 execution results post-merge
4. ✅ **Evaluate** GM2 eligibility using strict consecutive PASS rules
5. 🌿 **Create** backport branch(es) from the release branch
6. 🍒 **Cherry-pick** commits from develop onto the backport branch
7. 🔀 **Open** a Backport MR in GitLab automatically
8. 💬 **Post** the full approval comment to Jira with dynamic GM2 links
9. 🔄 **Transition** Jira workflow: `Running on GM2` → `GM Data Creation` → `MR to GM`

**All of this. One command.**

---

## 🧠 GM2 Eligibility Rules

The script evaluates up to the last 10 GM2 runs post-merge.
Only runs where `rerun_count = 0` and `isCBB = false` are counted.

| Run Pattern | Eligible? |
|---|---|
| ✅ PASS → PASS | **YES** — trigger immediately |
| ✅ FAIL → PASS → PASS | **YES** |
| ❌ PASS → FAIL → PASS | **NO** — preceding run failed |
| ❌ PASS → PASS → FAIL | **NO** — latest run failed |
| ❌ Less than 2 runs | **NO** — insufficient data |

> **Note:** All test cases in the MR must be eligible. One failure = DO NOT BACKPORT.

---

## 🔄 Jira Workflow Transitions (Auto)

| From | To | Trigger |
|---|---|---|
| `Running on GM2` | `GM Data Creation` | Script detects this status |
| `GM Data Creation` | `MR to GM` | Script chains immediately after |
| `MR to GM` | — | Already done, skipped |

The script detects the **current status** and applies only the remaining transitions. No manual Jira updates needed.

---

## 🛠️ Prerequisites

### 1. Python 3.8+
```powershell
python --version
```
*Must be 3.8 or higher.*

### 2. Install Dependency
```powershell
pip install requests
```
*That's the only dependency.*

### 3. Company VPN
⚠️ You MUST be connected to the company VPN. OpenSearch (`autoinfra-es.vaultdev.com`) is not accessible without it. The script will exit with a clear error if VPN is not connected.

### 4. Access Tokens
You need two tokens:

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
Copy the example and fill in your values:
```powershell
copy tools\backport\.env.example tools\backport\set_env.ps1
```

Edit `set_env.ps1`:
```powershell
$env:GITLAB_TOKEN = 'your-gitlab-pat-here'
$env:GITLAB_URL   = 'https://gitlab.veevadev.com'
$env:JIRA_URL     = 'https://jira.veevadev.com'
$env:JIRA_PAT     = 'your-jira-pat-here'
$env:OPENSEARCH_URL = 'https://autoinfra-es.vaultdev.com:9200'
```

🔒 `set_env.ps1` is in `.gitignore` — it will never be committed. Never share this file or paste its contents anywhere.

### Step 3 — Load credentials
Run this at the start of every PowerShell session:
```powershell
cd tools\backport
. .\set_env.ps1
```

### Step 4 — Verify setup
```powershell
# Test GitLab access
python backport.py --mr 91281
```
If you see the eligibility report — you're good to go. ✅

---

## 📝 MR Description Format (MANDATORY)

⚠️ Every develop MR MUST have this in the description before merging. Without it, the script cannot identify which test cases to validate in GM2.

Add this block to the MR description:
```text
Test Cases: TC-1234, TC-5678
Xray IDs: DEV-1085503
```
* **Test Cases** — test case IDs (comma separated)
* **Xray IDs** — Xray/DEV IDs (comma separated)
* You can have one or both lines
* IDs are used to query GM2 execution results from OpenSearch

---

## 🎯 Usage

Navigate to the script directory and load credentials:
```powershell
cd C:\Working_Env\BackportRequestAutomation\tools\backport
. .\set_env.ps1
```

### 🔍 Phase 1 — Dry Run (Read-Only, Safe to Run Anytime)
```powershell
python backport.py --mr <MR_IID>
```

Example:
```powershell
python backport.py --mr 91281
```

**What it does:**
* Fetches MR metadata
* Checks Jira Fix Version(s)
* Queries GM2 results and evaluates eligibility
* Lists commits to cherry-pick
* Checks for existing backport MRs
* Makes zero changes anywhere

**Example output:**
```text
============================================================
  BACKPORT ELIGIBILITY REPORT
============================================================

MR:            !91281
Title:         QA-558547 Script Update for GM and GM2 Failure
Author:        satyavinay.vella
Merged At:     2026-08-26T11:55:40.941Z
Jira:          QA-558547
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
  Fix Version:     26R2.2  |  Target: release/26.2.2  |  Branch: r26.2.2_gm/username/QA-558547_SU
  Fix Version:     26R2.3  |  Target: release/26.2.3  |  Branch: r26.2.3_gm/username/QA-558547_SU

Dry run complete. Run with --execute to create the backport MR and post Jira comment.
============================================================
```

### 🚀 Phase 2 — Full Execution
⚠️ Only run this after Phase 1 shows `Overall: BACKPORT`.

```powershell
python backport.py --mr <MR_IID> --execute
```

Example:
```powershell
python backport.py --mr 91281 --execute
```

**What it does:**
* Everything in Phase 1, plus:
* Creates backport branch(es) from release branch
* Cherry-picks commits (stops and cleans up on conflict)
* Opens Backport MR in GitLab
* Posts approval comment to Jira with GM2 run links
* Transitions Jira workflow automatically

### 🎯 Override Target Branch (Testing / Edge Cases)
Use this when you want to backport to a specific branch regardless of Jira Fix Version:
```powershell
python backport.py --mr <MR_IID> --execute --target-branch release/26.2.2
```

⚠️ If the override doesn't match the Jira Fix Version, the script will warn you and ask for confirmation before proceeding.

---

## 🚨 Error Scenarios & What To Do

| Error | Cause | Fix |
|---|---|---|
| Missing required environment variables | `set_env.ps1` not loaded | Run `. .\set_env.ps1` |
| Cannot reach OpenSearch | Not on VPN | Connect to company VPN |
| No Jira ID found in MR title | MR title missing QA-XXXXXX | Add Jira ID to MR title |
| No Test Cases or Xray IDs found | MR description missing IDs | Add `Xray IDs: DEV-XXXXXX` to MR description |
| No Fix Version set in Jira | Jira ticket missing Fix Version | Set Fix Version in Jira before running |
| Cherry-pick conflict | Changes conflict with release branch | Resolve manually, branch auto-deleted |
| DO NOT BACKPORT | GM2 runs don't meet eligibility | Wait for more GM2 runs and re-run |
| Backport MR already exists | Already created | Script skips creation, posts comment only |

---

## 🔁 Day-to-Day Workflow

```text
Developer merges MR into develop
         ↓
GM2 executions run automatically
         ↓
Run: python backport.py --mr <IID>
         ↓
Check: Overall: BACKPORT?
    YES → python backport.py --mr <IID> --execute
    NO  → Wait for more GM2 runs, re-run Phase 1
         ↓
Backport MR created in GitLab ✅
Jira comment posted ✅
Jira workflow transitioned ✅
         ↓
Manager (Your Manager) approves Jira comment
         ↓
Backport MR merged into release branch ✅
```

---

## 📁 Project Structure

```text
BackportRequestAutomation/
├── tools/
│   └── backport/
│       ├── backport.py       ← main script
│       ├── .env.example      ← credentials template (safe to commit)
│       ├── set_env.ps1       ← your credentials (NEVER commit this)
│       └── README.md         ← this file
└── .gitignore
```

---

## 🔒 Security Rules

* ✅ `set_env.ps1` is in `.gitignore` — never committed
* ✅ `.env` is in `.gitignore` — never committed
* ❌ Never paste tokens in MR descriptions, comments, or chat
* ❌ Never commit credentials in any file
* ❌ Never share `set_env.ps1` with anyone — each user creates their own

---

## 👥 Onboarding a New Team Member

1. Share this repo link
2. Ask them to generate their own GitLab PAT and Jira PAT
3. They follow Setup Steps 1–4 above
4. Done — they're ready to run backports

---

## 🆘 Need Help?

| Problem                      | Contact                       |
|------------------------------|-------------------------------|
| Script errors                | Check error table above first |
| GitLab token issues          | Your GitLab Profile           |
| Jira access issues           | Your Jira Profile             |
| OpenSearch / GM2 data issues | QA Infrastructure team        |
| Any Other Issue              | Contact Me: SatyaVinayVella   |