"""
Vercel Python serverless function: POST /api/analyze

Runs a pasted review through Azure AI Language and returns:
  - sentiment + confidence scores
  - key phrases
  - named entities
  - detected language
  - PII-redacted text
  - abstractive summary

Reads credentials from environment variables (set these in the Vercel
project's Settings -> Environment Variables, NOT committed to the repo):
  LANGUAGE_ENDPOINT = https://<your-resource>.cognitiveservices.azure.com
  LANGUAGE_KEY      = <key1 from your Azure AI Language resource>
"""

import json
import os
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

API_VERSION = "2023-04-01"
TIMEOUT_SECONDS = 20
MAX_CHARS = 5000

# Abstractive summarization is an async job. Poll settings:
SUMMARY_POLL_INTERVAL = 1.5   # seconds between polls
SUMMARY_MAX_WAIT = 25         # give up after this many seconds total


def _endpoint_and_key():
    return (
        os.environ.get("LANGUAGE_ENDPOINT", "").rstrip("/"),
        os.environ.get("LANGUAGE_KEY", ""),
    )


def _call_sync(endpoint: str, key: str, kind: str, text: str, language: str | None = "en") -> dict:
    """Synchronous /analyze-text call — used for sentiment, key phrases,
    entities, language detection, and PII recognition."""
    url = f"{endpoint}/language/:analyze-text?api-version={API_VERSION}"
    doc = {"id": "1", "text": text}
    if language:
        doc["language"] = language
    payload = {
        "kind": kind,
        "parameters": {"modelVersion": "latest"},
        "analysisInput": {"documents": [doc]},
    }
    return _post_json(url, key, payload)


def _call_summarization(endpoint: str, key: str, text: str) -> str:
    """Abstractive summarization is a long-running job: submit, then poll
    the operation-location URL until it finishes."""
    submit_url = f"{endpoint}/language/analyze-text/jobs?api-version={API_VERSION}"
    payload = {
        "analysisInput": {"documents": [{"id": "1", "language": "en", "text": text}]},
        "tasks": [
            {
                "kind": "AbstractiveSummarization",
                "parameters": {"sentenceCount": 3},
            }
        ],
    }
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        submit_url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Ocp-Apim-Subscription-Key": key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as resp:
            operation_url = resp.headers.get("operation-location")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")
        raise RuntimeError(f"AbstractiveSummarization submit failed: {exc.code} {detail}") from exc

    if not operation_url:
        raise RuntimeError("AbstractiveSummarization did not return an operation-location header")

    waited = 0.0
    while waited < SUMMARY_MAX_WAIT:
        poll_request = urllib.request.Request(
            operation_url,
            headers={"Ocp-Apim-Subscription-Key": key},
            method="GET",
        )
        with urllib.request.urlopen(poll_request, timeout=TIMEOUT_SECONDS) as resp:
            result = json.loads(resp.read().decode("utf-8"))

        status = result.get("status")
        if status == "succeeded":
            tasks = result.get("tasks", {}).get("items", [])
            for task in tasks:
                docs = task.get("results", {}).get("documents", [])
                if docs and docs[0].get("summaries"):
                    return docs[0]["summaries"][0]["text"]
            return ""
        if status == "failed":
            raise RuntimeError(f"AbstractiveSummarization job failed: {result}")

        time.sleep(SUMMARY_POLL_INTERVAL)
        waited += SUMMARY_POLL_INTERVAL

    raise RuntimeError("AbstractiveSummarization timed out waiting for the job to finish")


def _post_json(url: str, key: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Ocp-Apim-Subscription-Key": key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")
        raise RuntimeError(f"{url} failed: {exc.code} {detail}") from exc


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        endpoint, key = _endpoint_and_key()
        if not endpoint or not key:
            self._send_json(
                {"error": "Server is missing LANGUAGE_ENDPOINT / LANGUAGE_KEY environment variables."},
                500,
            )
            return

        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length) if content_length else b""
        try:
            body = json.loads(raw_body or b"{}")
        except ValueError:
            body = {}

        text = (body.get("text") or "").strip()
        if not text:
            self._send_json({"error": 'Request body must include non-empty "text".'}, 400)
            return
        if len(text) > MAX_CHARS:
            self._send_json({"error": f"Text must be {MAX_CHARS} characters or fewer."}, 400)
            return

        # Which sections the client wants back. Defaults to everything.
        features = body.get("features") or [
            "sentiment",
            "keyPhrases",
            "entities",
            "language",
            "pii",
            "summary",
        ]

        result = {}
        try:
            if "sentiment" in features:
                doc = _call_sync(endpoint, key, "SentimentAnalysis", text)["results"]["documents"][0]
                result["sentiment"] = doc["sentiment"]
                result["confidenceScores"] = doc["confidenceScores"]

            if "keyPhrases" in features:
                doc = _call_sync(endpoint, key, "KeyPhraseExtraction", text)["results"]["documents"][0]
                result["keyPhrases"] = doc.get("keyPhrases", [])

            if "entities" in features:
                doc = _call_sync(endpoint, key, "EntityRecognition", text)["results"]["documents"][0]
                result["entities"] = [
                    {"text": e["text"], "category": e["category"]}
                    for e in doc.get("entities", [])
                ]

            if "language" in features:
                # Language detection doesn't take a "language" hint on the document.
                doc = _call_sync(endpoint, key, "LanguageDetection", text, language=None)["results"]["documents"][0]
                detected = doc.get("detectedLanguage", {})
                result["language"] = {
                    "name": detected.get("name"),
                    "iso6391Name": detected.get("iso6391Name"),
                    "confidenceScore": detected.get("confidenceScore"),
                }

            if "pii" in features:
                doc = _call_sync(endpoint, key, "PiiEntityRecognition", text)["results"]["documents"][0]
                result["pii"] = {
                    "redactedText": doc.get("redactedText", text),
                    "entities": [
                        {"text": e["text"], "category": e["category"]}
                        for e in doc.get("entities", [])
                    ],
                }

            if "summary" in features:
                result["summary"] = _call_summarization(endpoint, key, text)

        except Exception as exc:  # noqa: BLE001
            self._send_json(
                {"error": f"Azure AI Language request failed: {exc}"},
                502,
            )
            return

        self._send_json(result, 200)

    def do_OPTIONS(self):
        # CORS preflight support, harmless to keep even for same-origin use.
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def _send_json(self, payload: dict, status: int):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self._send_cors_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
