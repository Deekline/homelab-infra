import json
import os
import urllib.request

with open("review.md") as f:
    body = f.read()

payload = json.dumps({"body": body}).encode()

repo = "deekline/homelab-infra"
pr_number = os.environ["PR_NUMBER"]

req = urllib.request.Request(
    f"http://10.0.10.150:30008/api/v1/repos/{repo}/issues/{pr_number}/comments",
    data=payload,
    headers={
        "Authorization": f"token {os.environ['RENOVATE_TOKEN']}",
        "content-type": "application/json",
    },
    method="POST",
)
urllib.request.urlopen(req)
