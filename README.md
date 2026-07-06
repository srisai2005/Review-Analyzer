# Review Analyser — HTML frontend + Python backend on Vercel

A single self-contained HTML page that grades a pasted review using
**Azure AI Language**: sentiment, key phrases, named entities, detected
language, PII redaction, and an abstractive summary — all from the same
Azure resource. Deployed on **Vercel**, with the frontend as static files
and the backend as a Python serverless function.

```
review-analyser/
├── public/
│   └── index.html          ← entire frontend: HTML + CSS + JS, one file
├── api/
│   └── analyze.py          ← POST /api/analyze — calls Azure AI Language
├── requirements.txt         ← empty on purpose (stdlib only), required by Vercel
├── vercel.json              ← raises the function timeout for summarization
├── .env.local.example       ← copy → .env.local for local dev
└── .gitignore
```

No build step. The frontend is plain HTML/CSS/JS in one file; the backend
is plain Python using only the standard library (`json`, `os`, `time`,
`urllib`, `http.server`).

---

## 1. What it does

Paste a review and click **Grade this review**. The backend sends the text
to Azure AI Language and returns:

- **Sentiment** — positive / negative / neutral / mixed, with confidence bars
- **Key phrases** — highlighted inline in the original text
- **Named entities** — people, places, organizations, etc.
- **Detected language** — name, ISO code, confidence
- **PII-redacted text** — names, emails, phone numbers, etc. blacked out
- **Abstractive summary** — a short plain-English summary (this one runs as
  an async job under the hood — the backend submits it and polls until it's
  ready, so it can take a few extra seconds)

---

## 2. Create the Azure AI Language resource (same as before)

```bash
az login
az account set --subscription "<your-subscription-name-or-id>"

RG="rg-review-analyser"
LOCATION="eastus"
LANG_NAME="lang-review-analyser"

az group create --name $RG --location $LOCATION

az cognitiveservices account create \
  --name $LANG_NAME \
  --resource-group $RG \
  --kind TextAnalytics \
  --sku F0 \
  --location $LOCATION \
  --yes

az cognitiveservices account show \
  --name $LANG_NAME --resource-group $RG \
  --query "properties.endpoint" -o tsv

az cognitiveservices account keys list \
  --name $LANG_NAME --resource-group $RG \
  --query "key1" -o tsv
```

> F0 (free) allows one instance per subscription per region. Abstractive
> summarization requires the resource to support the Summarization feature —
> this is generally available on standard (paid `S`) tier; check the Azure
> portal for current F0 feature availability if the summary call fails.

Portal equivalent: **Create a resource → "Language service"** → choose
Subscription/Resource group/Region → pick a pricing tier → Review + create →
**Keys and Endpoint** to copy them.

---

## 3. Push the code to GitHub

```bash
cd review-analyser
git init
git add .
git commit -m "Review Analyser: add language detection, PII redaction, summarization; move to Vercel"
git branch -M main
git remote add origin https://github.com/<your-username>/review-analyser.git
git push -u origin main
```

`.gitignore` already excludes `.env.local`, `.vercel/`, and Python cache
files, so your real key never gets committed.

---

## 4. Deploy to Vercel

### Option A — Vercel dashboard (easiest)

1. Go to [vercel.com/new](https://vercel.com/new) and import your GitHub repo.
2. Vercel auto-detects the `/api/analyze.py` file as a Python serverless
   function and `/public` as static output — no build command needed.
3. Before the first deploy (or right after), go to **Settings → Environment
   Variables** and add:
   - `LANGUAGE_ENDPOINT` = `https://<your-language-resource>.cognitiveservices.azure.com`
   - `LANGUAGE_KEY` = `<key1 from your Language resource>`
4. Click **Deploy**. Once it finishes, open the given `*.vercel.app` URL.

### Option B — Vercel CLI

```bash
npm install -g vercel
cd review-analyser
vercel login
vercel                     # first deploy, follow the prompts
vercel env add LANGUAGE_ENDPOINT production
vercel env add LANGUAGE_KEY production
vercel --prod              # redeploy with the env vars applied
```

**Never commit the key to the repo.** It only ever lives in Vercel's
environment variable settings (and, for local testing, in your git-ignored
`.env.local`).

Every push to `main` now auto-deploys via Vercel's GitHub integration —
no separate CI/CD file needed (unlike the old Azure Static Web Apps
workflow, Vercel wires this up automatically once the repo is imported).

---

## 5. Local development

```bash
npm install -g vercel
cd review-analyser
cp .env.local.example .env.local
# edit .env.local with your real endpoint/key
vercel dev
```

`vercel dev` serves `/public` and runs `api/analyze.py` locally, mirroring
production routing (`/api/analyze`) and env vars.

---

## 6. Cost / quota notes

- **Azure AI Language F0**: 5,000 text records/month free, then blocked
  until next cycle, or upgrade to `S` tier for summarization support.
- Each "Grade this review" click now costs up to **6 records** (sentiment,
  key phrases, entities, language detection, PII, summarization) against
  the monthly quota.
- **Vercel Hobby plan**: free, functions can run up to 60s (we configure
  30s in `vercel.json`), which comfortably covers the summarization job's
  polling loop.

---

## 7. Cleanup

```bash
az group delete --name $RG --yes --no-wait
```

Removes the Language resource and everything else in the resource group.
To remove the Vercel project, delete it from the Vercel dashboard under
**Settings → Delete Project**.
