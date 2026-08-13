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


rendered_chart_diff = ""
if os.path.exists("chart_render_diff.txt"):
    rendered_chart_diff = open("chart_render_diff.txt").read().strip()

# Apps already covered by a rendered manifest diff don't need the raw
# values.yaml dumped too — the render already shows how those values
# interact with the new chart, which is strictly stronger signal.
apps_with_rendered_diff = set(re.findall(r"^### (\S+):", rendered_chart_diff, re.M))

context_sections = []
for app_manifest in changed_app_manifests(diff):
    app_name = os.path.basename(app_manifest).removesuffix(".yaml")
    if app_name in apps_with_rendered_diff:
        continue
    for values_path in referenced_values_files(app_manifest):
        if not os.path.exists(values_path):
            continue
        content = open(values_path).read()[:MAX_VALUES_FILE_CHARS]
        context_sections.append(
            f"Current contents of {values_path} (the config this chart bump "
            f"will deploy against):\n```yaml\n{content}\n```"
        )

extra_context = "\n\n".join(context_sections)
if rendered_chart_diff:
    extra_context = (
        f"Rendered manifest diff (ground truth for what actually changes "
        f"in the cluster):\n\n{rendered_chart_diff}\n\n{extra_context}"
    )

prompt = (
    "You are reviewing an automated dependency-update pull request for a "
    "homelab Kubernetes GitOps repo (k3s, Flux CD, Helm charts, raw manifests). "
    "Write a concise PR review comment in markdown with three sections: "
    "**Summary** (what's changing and which app(s) are affected), "
    "**Risks** (breaking changes, major version bumps, config format changes, "
    "anything that could fail to deploy or fail at runtime), and "
    "**Recommendation** (safe to merge / merge with caution / needs manual "
    "testing before merge, with a one-line reason).\n\n"
    "Ground the Risks section in the actual context below, not generic "
    "version-bump boilerplate. If a rendered manifest diff is provided, "
    "that is ground truth for what actually changes in the cluster — base "
    "your risk assessment on it directly rather than speculating about "
    "what a chart version bump might do. If it shows no functional change, "
    "say so plainly. If only a values file is provided (no rendered diff), "
    "check whether its keys/structure are actually likely to conflict with "
    "the new version — cite the specific keys you're concerned about, or "
    "say there's nothing concerning instead of listing hypothetical risks "
    "you can't tie to what's actually configured here.\n\n"
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
