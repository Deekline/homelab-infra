import os
import re
import subprocess

MAX_SECTION_CHARS = 40000


def changed_app_manifests(diff_text):
    paths = re.findall(r"^\+\+\+ b/(apps/.+\.yaml)$", diff_text, re.M)
    return sorted(set(paths))


def file_diff_section(diff_text, path):
    marker = f"+++ b/{path}"
    idx = diff_text.find(marker)
    if idx == -1:
        return ""
    next_idx = diff_text.find("\ndiff --git", idx)
    return diff_text[idx: next_idx if next_idx != -1 else len(diff_text)]


def old_new_target_revision(section):
    old = re.search(r'^-\s*targetRevision:\s*"?([^"\n]+?)"?\s*$', section, re.M)
    new = re.search(r'^\+\s*targetRevision:\s*"?([^"\n]+?)"?\s*$', section, re.M)
    return (old.group(1) if old else None, new.group(1) if new else None)


def chart_sources(file_content):
    blocks = re.split(r"(?=^\s*-\s*repoURL:)", file_content, flags=re.M)
    sources = []
    for b in blocks:
        chart_m = re.search(r"^\s*chart:\s*(\S+)", b, re.M)
        repo_m = re.search(r"^\s*-\s*repoURL:\s*(\S+)", b, re.M)
        if chart_m and repo_m:
            sources.append({"chart": chart_m.group(1), "repoURL": repo_m.group(1)})
    return sources


def referenced_values_files(file_content):
    return re.findall(r"-\s*\$values/(k3s/\S+\.ya?ml)", file_content)


def helm_repo_alias(repo_url):
    return re.sub(r"\W+", "-", repo_url).strip("-")[:60]


def render(chart, repo_url, version, values_path):
    alias = helm_repo_alias(repo_url)
    subprocess.run(
        ["helm", "repo", "add", alias, repo_url, "--force-update"],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(["helm", "repo", "update", alias], check=True, capture_output=True, text=True)

    cmd = ["helm", "template", chart, f"{alias}/{chart}", "--version", version]
    if values_path and os.path.exists(values_path):
        cmd += ["-f", values_path]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[:2000])
    return result.stdout


def main():
    with open("pr.diff.truncated") as f:
        diff_text = f.read()

    sections = []
    for app_manifest in changed_app_manifests(diff_text):
        section = file_diff_section(diff_text, app_manifest)
        old_version, new_version = old_new_target_revision(section)
        if not old_version or not new_version:
            continue

        if not os.path.exists(app_manifest):
            continue
        content = open(app_manifest).read()
        sources = chart_sources(content)
        if not sources:
            continue
        source = sources[0]

        values_files = referenced_values_files(content)
        values_path = values_files[0] if values_files else None

        app_name = os.path.basename(app_manifest).removesuffix(".yaml")
        try:
            old_rendered = render(source["chart"], source["repoURL"], old_version, values_path)
            new_rendered = render(source["chart"], source["repoURL"], new_version, values_path)
        except Exception as exc:  # noqa: BLE001 - degrade gracefully, don't fail the job
            sections.append(
                f"### {app_name}: helm template render failed, could not diff "
                f"{source['chart']} {old_version} -> {new_version}\n{exc}"
            )
            continue

        with open(f"{app_name}-old.yaml", "w") as f:
            f.write(old_rendered)
        with open(f"{app_name}-new.yaml", "w") as f:
            f.write(new_rendered)

        diff_result = subprocess.run(
            ["diff", f"{app_name}-old.yaml", f"{app_name}-new.yaml"],
            capture_output=True, text=True,
        )
        rendered_diff = diff_result.stdout[:MAX_SECTION_CHARS]

        if not rendered_diff.strip():
            sections.append(
                f"### {app_name}: {source['chart']} {old_version} -> {new_version}\n"
                "Rendered manifests are IDENTICAL except for version-bump "
                "labels/annotations - no functional change to what gets deployed."
            )
        else:
            sections.append(
                f"### {app_name}: {source['chart']} {old_version} -> {new_version}\n"
                f"Actual diff in rendered Kubernetes manifests (this is the real "
                f"deploy-time impact, computed by rendering both chart versions "
                f"against the current values.yaml):\n```diff\n{rendered_diff}\n```"
            )

    with open("chart_render_diff.txt", "w") as f:
        f.write("\n\n".join(sections))


if __name__ == "__main__":
    main()
