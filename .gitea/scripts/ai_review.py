import json
import os
import re
import urllib.request

MAX_VALUES_FILE_CHARS = 30000

diff = open("pr.diff.truncated").read()


def changed_app_manifests(diff_text):
    paths = re.findall(r"^\+\+\+ b/(apps/.+\.yaml)$", diff_text, re.M)
    return sorted(set(paths))


def referenced_values_files(app_manifest_path):
    if not os.path.exists(app_manifest_path):
        return []
    content = open(app_manifest_path).read()
    # e.g. "- $values/k3s/loki/values.yaml" -> "k3s/loki/values.yaml"
    return re.findall(r"-\s*\$values/(k3s/\S+\.ya?ml)", content)


context_sections = []
for app_manifest in changed_app_manifests(diff):
    for values_path in referenced_values_files(app_manifest):
        if not os.path.exists(values_path):
            continue
        content = open(values_path).read()[:MAX_VALUES_FILE_CHARS]
        context_sections.append(
            f"Current contents of {values_path} (the config this chart bump "
            f"will deploy against):\n```yaml\n{content}\n```"
        )

extra_context = "\n\n".join(context_sections)

prompt = (
    "You are reviewing an automated dependency-update pull request for a "
    "homelab Kubernetes GitOps repo (k3s, ArgoCD, Helm charts, raw manifests). "
    "Write a concise PR review comment in markdown with three sections: "
    "**Summary** (what's changing and which app(s) are affected), "
    "**Risks** (breaking changes, major version bumps, config format changes, "
    "anything that could fail to deploy or fail at runtime), and "
    "**Recommendation** (safe to merge / merge with caution / needs manual "
    "testing before merge, with a one-line reason).\n\n"
    "Ground the Risks section in the actual files below, not generic "
    "version-bump boilerplate. If a values file is provided, check whether "
    "its keys/structure are actually likely to conflict with the new "
    "version — cite the specific keys you're concerned about, or say "
    "there's nothing concerning in the current config instead of listing "
    "hypothetical risks you can't tie to what's actually configured here.\n\n"
    f"Diff:\n{diff}"
)

if extra_context:
    prompt += f"\n\n{extra_context}"

payload = json.dumps({
    "model": "claude-sonnet-5",
    "max_tokens": 2048,
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

# Sonnet 5 runs adaptive thinking by default, so content[0] may be a
# thinking block rather than text — find the text block instead of
# assuming position 0.
text_blocks = [block["text"] for block in result["content"] if block["type"] == "text"]
review = "\n\n".join(text_blocks)

with open("review.md", "w") as f:
    f.write(review)
