import json
import os
import urllib.request

diff = open("pr.diff.truncated").read()

prompt = (
    "You are reviewing an automated dependency-update pull request for a "
    "homelab Kubernetes GitOps repo (k3s, ArgoCD, Helm charts, raw manifests). "
    "Write a concise PR review comment in markdown with three sections: "
    "**Summary** (what's changing and which app(s) are affected), "
    "**Risks** (breaking changes, major version bumps, config format changes, "
    "anything that could fail to deploy or fail at runtime), and "
    "**Recommendation** (safe to merge / merge with caution / needs manual "
    "testing before merge, with a one-line reason).\n\n"
    f"Diff:\n{diff}"
)

payload = json.dumps({
    "model": "claude-sonnet-5",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": prompt}],
}).encode()

req = urllib.request.Request(
    "https://api.anthropic.com/v1/messages",
    data=payload,
    headers={
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    },
)
with urllib.request.urlopen(req) as resp:
    result = json.load(resp)

review = result["content"][0]["text"]
with open("review.md", "w") as f:
    f.write(review)
